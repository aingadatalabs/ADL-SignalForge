"""DuckDB-backed enrichment cache, .env auto-loading, ordered fallback, and credit guardrails."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

from .base import EnrichmentResult, VerificationStatus, normalize_domain, normalize_email


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def slugify_person(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return cleaned


def normalize_linkedin_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip().lower().split("?", 1)[0].rstrip("/")
    for prefix in ("https://", "http://", "www."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned or None


def _normalize_operation(operation: str) -> str:
    op = operation.strip().lower()
    aliases = {
        "company": "company",
        "enrich_company": "company",
        "contacts": "contacts",
        "find_contacts": "contacts",
        "contact": "contacts",
        "email_verify": "email_verify",
        "verify_email": "email_verify",
        "verify": "email_verify",
        "tech_signals": "tech_signals",
        "dns_tech": "tech_signals",
        "technographics": "tech_signals",
    }
    return aliases.get(op, op)


def _canonical_lookup_fields(
    *,
    operation: str,
    domain: str | None = None,
    linkedin_url: str | None = None,
    person_name: str | None = None,
    email: str | None = None,
) -> tuple[str, str, str, str, str]:
    """Normalize which identity fields participate in cache keys per operation."""
    op = _normalize_operation(operation)
    norm_domain = normalize_domain(domain) or ""
    norm_li = normalize_linkedin_url(linkedin_url) or ""
    person_slug = slugify_person(person_name)
    norm_email = normalize_email(email) or ""

    if op in {"company", "tech_signals"}:
        # Company & tech/DNS lookups key on domain (or LinkedIn URL if domain is absent);
        # person/email hints do not split the cached entry.
        key_domain = norm_domain if norm_domain else ""
        key_li = "" if norm_domain else norm_li
        return op, key_domain, key_li, "", ""

    if op == "email_verify":
        # Email verification keys primarily on the normalized email address.
        return op, "", "", "", norm_email

    if op == "contacts":
        # Contact lookups key on domain + person/LinkedIn/email, avoiding duplicate
        # entries when an email is added or omitted for the same person on a domain.
        return op, norm_domain, norm_li, person_slug, norm_email

    return op, norm_domain, norm_li, person_slug, norm_email


def build_cache_key(
    *,
    operation: str,
    domain: str | None = None,
    linkedin_url: str | None = None,
    person_name: str | None = None,
    email: str | None = None,
) -> str:
    """Deterministic key scoped by operation and normalized lookup fields."""
    op, key_domain, key_li, person_slug, norm_email = _canonical_lookup_fields(
        operation=operation,
        domain=domain,
        linkedin_url=linkedin_url,
        person_name=person_name,
        email=email,
    )
    parts = [
        f"op={op}",
        f"dom={key_domain}",
        f"li={key_li}",
        f"person={person_slug}",
        f"email={norm_email}",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_and_escape_sql_path(path: Path) -> str:
    """Resolve and validate a filesystem path for safe SQL COPY literal usage."""
    resolved = path.resolve()
    posix_str = resolved.as_posix()
    if "\x00" in posix_str or any(ord(ch) < 32 for ch in posix_str):
        raise ValueError(f"Invalid control characters in export path: {posix_str!r}")
    # Escape single quotes for SQL string literal context
    return posix_str.replace("'", "''")


# Ordered strictly 1 -> last matching your .env sequence
ENV_PROVIDER_ORDER: tuple[tuple[str, str], ...] = (
    ("icypeas", "icypeas_API_KEY"),
    ("findymail", "findymail_API_KEY"),
    ("lusha", "lusha.com_API_KEY"),
    ("rocketreach", "rocketreach.co_API_KEY"),
    ("tranco", "tranco_API_KEY"),
    ("fullenrich", "fullenrich.com_API_KEY"),
    ("bettercontact", "bettercontact.rocks_API_KEY"),
    ("clay", "clay.com_API_KEY"),
    ("thecompaniesapi", "thecompaniesapi.com_API_KEY"),
    ("builtwith", "builtwith.com_API_KEY"),
    ("wappalyzer", "wappalyzer.com_API_KEY"),
    ("snov", "snov.io_API_KEY"),
    ("prospeo", "prospeo.io_API_KEY"),
    ("apollo", "apollo.io_API_KEY"),
)

ENV_PROVIDER_MAP: dict[str, str] = dict(ENV_PROVIDER_ORDER)

DEFAULT_OPERATION_TTLS: dict[str, timedelta] = {
    "company": timedelta(days=90),
    "tech_signals": timedelta(days=21),
    "contacts": timedelta(days=45),
    "email_verify": timedelta(days=14),
}


def load_configured_providers(
    env: dict[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    autoload_dotenv: bool = True,
) -> dict[str, str]:
    """Load .env if os.environ lacks keys, then return configured providers in 1..N order."""
    if env is None and autoload_dotenv:
        resolved_dotenv = (
            Path(dotenv_path) if dotenv_path else Path(__file__).resolve().parents[1] / ".env"
        )
        if resolved_dotenv.exists():
            # override=False ensures existing os.environ entries take precedence
            load_dotenv(dotenv_path=resolved_dotenv, override=False)

    source = env if env is not None else os.environ
    configured: dict[str, str] = {}
    for provider, env_var in ENV_PROVIDER_ORDER:
        val = (source.get(env_var) or "").strip()
        if val:
            configured[provider] = val
    return configured


@dataclass
class CreditBudgetExceeded(RuntimeError):
    provider: str
    requested: float
    remaining: float


@dataclass
class FallbackAttempt:
    rank: int
    provider: str
    status: str  # "hit", "skipped_no_key", "skipped_budget", "failed", "empty", "escalated"
    detail: str | None = None


def _result_has_valid_email(result: EnrichmentResult) -> bool:
    """Return True if a result carries an explicitly VALID email or contact email."""
    if result.email_verified_status == VerificationStatus.VALID:
        return True
    if result.verified_status == VerificationStatus.VALID and any(
        normalize_email(c.email) for c in result.contacts
    ):
        return True
    return any(
        normalize_email(c.email) and c.verified_status == VerificationStatus.VALID
        for c in result.contacts
    )


def _default_should_continue_fallback(
    result: EnrichmentResult,
    *,
    min_confidence: float = 0.0,
    require_valid_email: bool = False,
) -> bool:
    """Decide whether a usable result is strong enough to stop or should escalate to later providers."""
    if min_confidence > 0.0 and result.confidence < min_confidence:
        return True
    if require_valid_email and not _result_has_valid_email(result):
        return True
    return False


class EnrichmentCache:
    """Caches enrichment payloads, auto-loads .env, and runs ordered 1..N provider fallback."""

    def __init__(
        self,
        db_path: str | Path = "storage/enrichment_cache.duckdb",
        *,
        dotenv_path: str | Path | None = None,
        autoload_dotenv: bool = True,
        default_ttl_days: int = 45,
        operation_ttls: dict[str, timedelta] | None = None,
        max_run_credits: float = 25.0,
        max_daily_credits: float | None = None,
        max_total_credits: float | None = None,
        provider_credit_caps: dict[str, float] | None = None,
        provider_daily_caps: dict[str, float] | None = None,
        provider_total_caps: dict[str, float] | None = None,
        provider_order: list[str] | tuple[str, ...] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = timedelta(days=default_ttl_days)
        self.operation_ttls: dict[str, timedelta] = {
            **{_normalize_operation(k): v for k, v in DEFAULT_OPERATION_TTLS.items()},
            **{_normalize_operation(k): v for k, v in (operation_ttls or {}).items()},
        }
        self.max_run_credits = max_run_credits
        self.max_daily_credits = max_daily_credits
        self.max_total_credits = max_total_credits
        self.provider_credit_caps = dict(provider_credit_caps or {})
        self.provider_daily_caps = dict(provider_daily_caps or {})
        self.provider_total_caps = dict(provider_total_caps or {})
        self.run_id = run_id or _utc_now().strftime("%Y%m%dT%H%M%SZ")

        self._run_credits_used: float = 0.0
        self._provider_run_credits: dict[str, float] = {}
        self._daily_credits_used: float = 0.0
        self._provider_daily_credits: dict[str, float] = {}
        self._total_credits_used: float = 0.0
        self._provider_total_credits: dict[str, float] = {}
        self._tracked_day: str = _utc_now().strftime("%Y-%m-%d")

        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # Serialize DuckDB access across threads and async tasks on this cache instance
        self._db_lock = threading.RLock()
        self._async_lock: asyncio.Lock | None = None
        self._conn: duckdb.DuckDBPyConnection | None = None

        self.configured_keys = load_configured_providers(
            dotenv_path=dotenv_path,
            autoload_dotenv=autoload_dotenv,
        )
        # Default order follows 1..last in ENV_PROVIDER_ORDER; custom list can subset/reorder if needed
        self.provider_order: list[str] = (
            list(provider_order)
            if provider_order is not None
            else [name for name, _ in ENV_PROVIDER_ORDER]
        )
        self._init_db()
        self._hydrate_historical_spend()

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _get_conn(self) -> duckdb.DuckDBPyConnection:
        with self._db_lock:
            if self._conn is None:
                self._conn = duckdb.connect(str(self.db_path))
            return self._conn

    def close(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        with self._db_lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_cache (
                    cache_key VARCHAR PRIMARY KEY,
                    operation VARCHAR NOT NULL,
                    domain VARCHAR,
                    linkedin_url VARCHAR,
                    person_slug VARCHAR,
                    email VARCHAR,
                    provider VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    expires_at TIMESTAMP NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    cache_key VARCHAR,
                    credits_used DOUBLE NOT NULL,
                    recorded_at TIMESTAMP NOT NULL
                );
                """
            )

    def _hydrate_historical_spend(self) -> None:
        """Load persisted run, daily, and total historical spend from credit_ledger."""
        with self._db_lock:
            conn = self._get_conn()
            now = _utc_now()
            current_day = now.strftime("%Y-%m-%d")
            day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            next_day_start = day_start + timedelta(days=1)

            self._tracked_day = current_day
            self._run_credits_used = 0.0
            self._provider_run_credits = {}
            self._daily_credits_used = 0.0
            self._provider_daily_credits = {}
            self._total_credits_used = 0.0
            self._provider_total_credits = {}

            total_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(credits_used), 0.0)
                FROM credit_ledger
                GROUP BY provider
                """
            ).fetchall()
            for provider, total_val in total_rows:
                val = round(float(total_val or 0.0), 6)
                self._provider_total_credits[str(provider)] = val
                self._total_credits_used = round(self._total_credits_used + val, 6)

            daily_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(credits_used), 0.0)
                FROM credit_ledger
                WHERE recorded_at >= ? AND recorded_at < ?
                GROUP BY provider
                """,
                [day_start, next_day_start],
            ).fetchall()
            for provider, daily_val in daily_rows:
                val = round(float(daily_val or 0.0), 6)
                self._provider_daily_credits[str(provider)] = val
                self._daily_credits_used = round(self._daily_credits_used + val, 6)

            run_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(credits_used), 0.0)
                FROM credit_ledger
                WHERE run_id = ?
                GROUP BY provider
                """,
                [self.run_id],
            ).fetchall()
            for provider, run_val in run_rows:
                val = round(float(run_val or 0.0), 6)
                self._provider_run_credits[str(provider)] = val
                self._run_credits_used = round(self._run_credits_used + val, 6)

    def _roll_daily_window_if_needed(self) -> None:
        """Reset in-memory daily counters when UTC calendar day rolls over."""
        current_day = _utc_now().strftime("%Y-%m-%d")
        if current_day != self._tracked_day:
            self._tracked_day = current_day
            self._daily_credits_used = 0.0
            self._provider_daily_credits = {}

    def ttl_for_operation(self, operation: str, ttl: timedelta | None = None) -> timedelta:
        """Resolve effective TTL from explicit override -> operation_ttls -> default_ttl."""
        if ttl is not None:
            return ttl
        norm_op = _normalize_operation(operation)
        return self.operation_ttls.get(norm_op, self.default_ttl)

    def has_key(self, provider: str) -> bool:
        return provider in self.configured_keys

    def get_key(self, provider: str) -> str | None:
        return self.configured_keys.get(provider)

    def ordered_providers(self, *, only_configured: bool = False) -> list[str]:
        if not only_configured:
            return list(self.provider_order)
        return [p for p in self.provider_order if self.has_key(p)]

    def _lookup_row(
        self,
        *,
        operation: str,
        domain: str | None = None,
        linkedin_url: str | None = None,
        person_name: str | None = None,
        email: str | None = None,
    ) -> tuple[str, Any, Any] | None:
        key = build_cache_key(
            operation=operation,
            domain=domain,
            linkedin_url=linkedin_url,
            person_name=person_name,
            email=email,
        )
        with self._db_lock:
            conn = self._get_conn()
            row = conn.execute(
                """
                SELECT cache_key, payload_json, expires_at
                FROM enrichment_cache
                WHERE cache_key = ?
                """,
                [key],
            ).fetchone()
            if row:
                return row[0], row[1], row[2]

            # Secondary lookup for contact queries: if email was supplied on a later call
            # for the same domain + person/LinkedIn where email was previously unknown,
            # reuse the matching contact entry rather than spending extra credits.
            op, norm_domain, norm_li, person_slug, _ = _canonical_lookup_fields(
                operation=operation,
                domain=domain,
                linkedin_url=linkedin_url,
                person_name=person_name,
                email=email,
            )
            if op == "contacts" and (person_slug or norm_li):
                if person_slug and norm_domain:
                    alt = conn.execute(
                        """
                        SELECT cache_key, payload_json, expires_at
                        FROM enrichment_cache
                        WHERE operation = 'contacts'
                          AND domain = ?
                          AND person_slug = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        [norm_domain, person_slug],
                    ).fetchone()
                    if alt:
                        return alt[0], alt[1], alt[2]
                if norm_li:
                    alt_li = conn.execute(
                        """
                        SELECT cache_key, payload_json, expires_at
                        FROM enrichment_cache
                        WHERE operation = 'contacts'
                          AND linkedin_url = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        [norm_li],
                    ).fetchone()
                    if alt_li:
                        return alt_li[0], alt_li[1], alt_li[2]

        return None

    def get(
        self,
        *,
        operation: str,
        domain: str | None = None,
        linkedin_url: str | None = None,
        person_name: str | None = None,
        email: str | None = None,
        allow_expired: bool = False,
    ) -> EnrichmentResult | None:
        with self._db_lock:
            found = self._lookup_row(
                operation=operation,
                domain=domain,
                linkedin_url=linkedin_url,
                person_name=person_name,
                email=email,
            )
            if not found:
                self._cache_misses += 1
                return None

            _, payload_json, expires_at = found
            now = _utc_now()
            if isinstance(expires_at, str):
                expires_dt = datetime.fromisoformat(expires_at)
            else:
                expires_dt = expires_at
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)

            if not allow_expired and now >= expires_dt:
                self._cache_misses += 1
                return None

            self._cache_hits += 1
            return EnrichmentResult.model_validate_json(payload_json)

    def put(
        self,
        result: EnrichmentResult,
        *,
        operation: str,
        domain: str | None = None,
        linkedin_url: str | None = None,
        person_name: str | None = None,
        email: str | None = None,
        ttl: timedelta | None = None,
    ) -> str:
        op, key_domain, key_li, person_slug, norm_email = _canonical_lookup_fields(
            operation=operation,
            domain=domain,
            linkedin_url=linkedin_url,
            person_name=person_name,
            email=email,
        )
        key = build_cache_key(
            operation=op,
            domain=domain,
            linkedin_url=linkedin_url,
            person_name=person_name,
            email=email,
        )
        now = _utc_now()
        effective_ttl = self.ttl_for_operation(op, ttl=ttl)
        expires_at = now + effective_ttl

        with self._db_lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO enrichment_cache (
                    cache_key, operation, domain, linkedin_url, person_slug, email,
                    provider, payload_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    op,
                    key_domain or None,
                    key_li or None,
                    person_slug or None,
                    norm_email or None,
                    result.source,
                    result.model_dump_json(),
                    now,
                    expires_at,
                ],
            )
        return key

    def _remaining_budget(self, provider: str) -> float:
        with self._db_lock:
            self._roll_daily_window_if_needed()
            rem_list: list[float] = [max(0.0, self.max_run_credits - self._run_credits_used)]

            if self.max_daily_credits is not None:
                rem_list.append(max(0.0, self.max_daily_credits - self._daily_credits_used))
            if self.max_total_credits is not None:
                rem_list.append(max(0.0, self.max_total_credits - self._total_credits_used))

            run_cap = self.provider_credit_caps.get(provider)
            if run_cap is not None:
                rem_list.append(max(0.0, run_cap - self._provider_run_credits.get(provider, 0.0)))

            daily_cap = self.provider_daily_caps.get(provider)
            if daily_cap is not None:
                rem_list.append(
                    max(0.0, daily_cap - self._provider_daily_credits.get(provider, 0.0))
                )

            total_cap = self.provider_total_caps.get(provider)
            if total_cap is not None:
                rem_list.append(
                    max(0.0, total_cap - self._provider_total_credits.get(provider, 0.0))
                )

            return min(rem_list)

    def can_spend(self, provider: str, estimated_credits: float) -> bool:
        if estimated_credits <= 0:
            return True
        with self._db_lock:
            return estimated_credits <= self._remaining_budget(provider)

    def record_spend(
        self,
        provider: str,
        credits_used: float,
        *,
        cache_key: str | None = None,
        strict: bool = False,
    ) -> None:
        if credits_used < 0:
            raise ValueError("credits_used cannot be negative")
        with self._db_lock:
            self._roll_daily_window_if_needed()
            if strict and not self.can_spend(provider, credits_used):
                remaining = self._remaining_budget(provider)
                raise CreditBudgetExceeded(
                    provider=provider, requested=credits_used, remaining=remaining
                )

            self._run_credits_used = round(self._run_credits_used + credits_used, 6)
            self._provider_run_credits[provider] = round(
                self._provider_run_credits.get(provider, 0.0) + credits_used, 6
            )
            self._daily_credits_used = round(self._daily_credits_used + credits_used, 6)
            self._provider_daily_credits[provider] = round(
                self._provider_daily_credits.get(provider, 0.0) + credits_used, 6
            )
            self._total_credits_used = round(self._total_credits_used + credits_used, 6)
            self._provider_total_credits[provider] = round(
                self._provider_total_credits.get(provider, 0.0) + credits_used, 6
            )

            now = _utc_now()
            entry_id = hashlib.sha256(
                f"{self.run_id}:{provider}:{now.isoformat()}:{self._total_credits_used}".encode()
            ).hexdigest()[:16]

            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO credit_ledger (id, run_id, provider, cache_key, credits_used, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [entry_id, self.run_id, provider, cache_key, credits_used, now],
            )

    async def resolve_with_fallback(
        self,
        *,
        operation: str,
        call_provider: Callable[[str, str], Awaitable[EnrichmentResult | None]],
        estimated_credits_fn: Callable[[str], float] | None = None,
        is_usable_result: Callable[[EnrichmentResult], bool] | None = None,
        should_continue_fallback: Callable[[EnrichmentResult], bool] | None = None,
        min_confidence: float = 0.0,
        require_valid_email: bool = False,
        domain: str | None = None,
        linkedin_url: str | None = None,
        person_name: str | None = None,
        email: str | None = None,
        force_refresh: bool = False,
        ttl: timedelta | None = None,
    ) -> tuple[EnrichmentResult | None, list[FallbackAttempt]]:
        """
        Check cache first; on miss, try providers in exact 1..N order.

        Stops at the first usable result unless `should_continue_fallback` (or
        `min_confidence` / `require_valid_email`) indicates a later provider should
        be tried for higher confidence or a VALID email. If later providers do not
        improve on a usable candidate, retains the best usable result.
        """
        attempts: list[FallbackAttempt] = []
        async_lock = self._get_async_lock()

        async with async_lock:
            if not force_refresh:
                cached = self.get(
                    operation=operation,
                    domain=domain,
                    linkedin_url=linkedin_url,
                    person_name=person_name,
                    email=email,
                )
                if cached is not None:
                    cached_needs_escalation = (
                        should_continue_fallback(cached)
                        if should_continue_fallback is not None
                        else _default_should_continue_fallback(
                            cached,
                            min_confidence=min_confidence,
                            require_valid_email=require_valid_email,
                        )
                    )
                    if not cached_needs_escalation:
                        attempts.append(
                            FallbackAttempt(
                                rank=0,
                                provider=cached.source,
                                status="hit",
                                detail="cache_hit",
                            )
                        )
                        return cached, attempts

            usable_fn = is_usable_result or (
                lambda r: bool(r.company_patch or r.contacts or r.email_verified_status is not None)
            )
            continue_fn = should_continue_fallback or (
                lambda r: _default_should_continue_fallback(
                    r,
                    min_confidence=min_confidence,
                    require_valid_email=require_valid_email,
                )
            )

            best_usable: EnrichmentResult | None = None
            best_cache_key: str | None = None
            best_rank: int = 0

            for rank, provider in enumerate(self.provider_order, start=1):
                api_key = self.get_key(provider)
                if not api_key:
                    attempts.append(
                        FallbackAttempt(rank=rank, provider=provider, status="skipped_no_key")
                    )
                    continue

                est_cost = estimated_credits_fn(provider) if estimated_credits_fn else 1.0
                if not self.can_spend(provider, est_cost):
                    attempts.append(
                        FallbackAttempt(
                            rank=rank,
                            provider=provider,
                            status="skipped_budget",
                            detail=f"est_cost={est_cost}",
                        )
                    )
                    continue

                try:
                    result = await call_provider(provider, api_key)
                except Exception as exc:
                    attempts.append(
                        FallbackAttempt(
                            rank=rank,
                            provider=provider,
                            status="failed",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue

                if result is None or not usable_fn(result):
                    attempts.append(
                        FallbackAttempt(
                            rank=rank,
                            provider=provider,
                            status="empty",
                            detail="no usable enrichment data returned",
                        )
                    )
                    continue

                actual_cost = result.credits_used if result.credits_used > 0 else est_cost
                cache_key = self.put(
                    result,
                    operation=operation,
                    domain=domain,
                    linkedin_url=linkedin_url,
                    person_name=person_name,
                    email=email,
                    ttl=ttl,
                )
                self.record_spend(provider, actual_cost, cache_key=cache_key)

                # Promote best_usable if this candidate is stronger (valid email or higher confidence)
                is_better = (
                    best_usable is None
                    or (_result_has_valid_email(result) and not _result_has_valid_email(best_usable))
                    or (
                        _result_has_valid_email(result) == _result_has_valid_email(best_usable)
                        and result.confidence > best_usable.confidence
                    )
                )
                if is_better:
                    best_usable = result
                    best_cache_key = cache_key
                    best_rank = rank

                if continue_fn(result):
                    attempts.append(
                        FallbackAttempt(
                            rank=rank,
                            provider=provider,
                            status="escalated",
                            detail=(
                                f"cached_key={cache_key[:12]} conf={result.confidence:.2f} "
                                f"valid_email={_result_has_valid_email(result)}"
                            ),
                        )
                    )
                    continue

                attempts.append(
                    FallbackAttempt(
                        rank=rank,
                        provider=provider,
                        status="hit",
                        detail=f"cached_key={cache_key[:12]}",
                    )
                )
                return result, attempts

            if best_usable is not None:
                attempts.append(
                    FallbackAttempt(
                        rank=best_rank,
                        provider=best_usable.source,
                        status="hit",
                        detail=(
                            f"best_usable_key={best_cache_key[:12]}"
                            if best_cache_key
                            else "best_usable_retained"
                        ),
                    )
                )
                return best_usable, attempts

        return None, attempts

    def stats(self) -> dict[str, Any]:
        """Return cache hit/miss counts, per-provider spend, and active row counts."""
        with self._db_lock:
            self._roll_daily_window_if_needed()
            conn = self._get_conn()
            now = _utc_now()

            total_rows = int(
                conn.execute("SELECT COUNT(*) FROM enrichment_cache").fetchone()[0] or 0
            )
            active_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM enrichment_cache WHERE expires_at > ?",
                    [now],
                ).fetchone()[0]
                or 0
            )
            expired_rows = max(0, total_rows - active_rows)

            ledger_rows = conn.execute(
                """
                SELECT provider, COALESCE(SUM(credits_used), 0.0)
                FROM credit_ledger
                GROUP BY provider
                ORDER BY provider
                """
            ).fetchall()
            ledger_credits_by_provider = {
                str(provider): round(float(total_credits or 0.0), 6)
                for provider, total_credits in ledger_rows
            }

            total_lookups = self._cache_hits + self._cache_misses
            hit_rate = round(self._cache_hits / total_lookups, 4) if total_lookups > 0 else 0.0

            return {
                "run_id": self.run_id,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "cache_hit_rate": hit_rate,
                "active_rows": active_rows,
                "expired_rows": expired_rows,
                "total_rows": total_rows,
                "run_credits_used": round(self._run_credits_used, 6),
                "daily_credits_used": round(self._daily_credits_used, 6),
                "total_credits_used": round(self._total_credits_used, 6),
                "credits_used_by_provider": dict(
                    sorted(self._provider_run_credits.items())
                ),
                "daily_credits_used_by_provider": dict(
                    sorted(self._provider_daily_credits.items())
                ),
                "total_credits_used_by_provider": ledger_credits_by_provider,
            }

    def export_parquet(self, output_path: str | Path = "storage/enrichment_cache.parquet") -> Path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        escaped_path = _validate_and_escape_sql_path(out)
        with self._db_lock:
            conn = self._get_conn()
            conn.execute(
                f"COPY enrichment_cache TO '{escaped_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return out.resolve()

    def export_jsonl(self, output_path: str | Path = "storage/enrichment_cache.jsonl") -> Path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._db_lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT cache_key, operation, domain, linkedin_url, person_slug, email, provider, payload_json, created_at, expires_at FROM enrichment_cache"
            ).fetchall()
        with out.open("w", encoding="utf-8") as fh:
            for r in rows:
                record = {
                    "cache_key": r[0],
                    "operation": r[1],
                    "domain": r[2],
                    "linkedin_url": r[3],
                    "person_slug": r[4],
                    "email": r[5],
                    "provider": r[6],
                    "payload": json.loads(r[7]),
                    "created_at": str(r[8]),
                    "expires_at": str(r[9]),
                }
                fh.write(json.dumps(record, default=str) + "\n")
        return out

__all__ = [
    "CreditBudgetExceeded",
    "DEFAULT_OPERATION_TTLS",
    "ENV_PROVIDER_MAP",
    "ENV_PROVIDER_ORDER",
    "EnrichmentCache",
    "FallbackAttempt",
    "build_cache_key",
    "load_configured_providers",
    "normalize_linkedin_url",
    "slugify_person",
]