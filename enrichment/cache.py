"""DuckDB-backed enrichment cache, .env auto-loading, ordered fallback, and credit guardrails."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv

from .base import EnrichmentResult, normalize_domain, normalize_email


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


def build_cache_key(
    *,
    operation: str,
    domain: str | None = None,
    linkedin_url: str | None = None,
    person_name: str | None = None,
    email: str | None = None,
) -> str:
    """Deterministic key across domain, LinkedIn URL, person slug, and email."""
    parts = [
        f"op={operation.strip().lower()}",
        f"dom={normalize_domain(domain) or ''}",
        f"li={normalize_linkedin_url(linkedin_url) or ''}",
        f"person={slugify_person(person_name)}",
        f"email={normalize_email(email) or ''}",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def load_configured_providers(
    env: dict[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    autoload_dotenv: bool = True,
) -> dict[str, str]:
    """Load .env if os.environ lacks keys, then return configured providers in 1..N order."""
    if env is None and autoload_dotenv:
        resolved_dotenv = Path(dotenv_path) if dotenv_path else Path(__file__).resolve().parents[1] / ".env"
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
    status: str  # "hit", "skipped_no_key", "skipped_budget", "failed", "empty"
    detail: str | None = None


class EnrichmentCache:
    """Caches enrichment payloads, auto-loads .env, and runs ordered 1..N provider fallback."""

    def __init__(
        self,
        db_path: str | Path = "storage/enrichment_cache.duckdb",
        *,
        dotenv_path: str | Path | None = None,
        autoload_dotenv: bool = True,
        default_ttl_days: int = 45,
        max_run_credits: float = 25.0,
        provider_credit_caps: dict[str, float] | None = None,
        provider_order: list[str] | tuple[str, ...] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = timedelta(days=default_ttl_days)
        self.max_run_credits = max_run_credits
        self.provider_credit_caps = dict(provider_credit_caps or {})
        self.run_id = run_id or _utc_now().strftime("%Y%m%dT%H%M%SZ")
        self._run_credits_used: float = 0.0
        self._provider_run_credits: dict[str, float] = {}

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

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.db_path))

    def _init_db(self) -> None:
        with self._connect() as conn:
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

    def has_key(self, provider: str) -> bool:
        return provider in self.configured_keys

    def get_key(self, provider: str) -> str | None:
        return self.configured_keys.get(provider)

    def ordered_providers(self, *, only_configured: bool = False) -> list[str]:
        if not only_configured:
            return list(self.provider_order)
        return [p for p in self.provider_order if self.has_key(p)]

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
        key = build_cache_key(
            operation=operation,
            domain=domain,
            linkedin_url=linkedin_url,
            person_name=person_name,
            email=email,
        )
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json, expires_at
                FROM enrichment_cache
                WHERE cache_key = ?
                """,
                [key],
            ).fetchone()

        if not row:
            return None

        payload_json, expires_at = row
        if isinstance(expires_at, str):
            expires_dt = datetime.fromisoformat(expires_at)
        else:
            expires_dt = expires_at
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)

        if not allow_expired and now >= expires_dt:
            return None

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
        key = build_cache_key(
            operation=operation,
            domain=domain,
            linkedin_url=linkedin_url,
            person_name=person_name,
            email=email,
        )
        now = _utc_now()
        expires_at = now + (ttl if ttl is not None else self.default_ttl)
        norm_domain = normalize_domain(domain)
        norm_li = normalize_linkedin_url(linkedin_url)
        person_slug = slugify_person(person_name)
        norm_email = normalize_email(email)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO enrichment_cache (
                    cache_key, operation, domain, linkedin_url, person_slug, email,
                    provider, payload_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    key,
                    operation,
                    norm_domain,
                    norm_li,
                    person_slug,
                    norm_email,
                    result.source,
                    result.model_dump_json(),
                    now,
                    expires_at,
                ],
            )
        return key

    def can_spend(self, provider: str, estimated_credits: float) -> bool:
        if estimated_credits <= 0:
            return True
        if self._run_credits_used + estimated_credits > self.max_run_credits:
            return False
        cap = self.provider_credit_caps.get(provider)
        if cap is not None:
            used = self._provider_run_credits.get(provider, 0.0)
            if used + estimated_credits > cap:
                return False
        return True

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
        if strict and not self.can_spend(provider, credits_used):
            remaining = max(0.0, self.max_run_credits - self._run_credits_used)
            raise CreditBudgetExceeded(provider=provider, requested=credits_used, remaining=remaining)

        self._run_credits_used = round(self._run_credits_used + credits_used, 6)
        self._provider_run_credits[provider] = round(
            self._provider_run_credits.get(provider, 0.0) + credits_used, 6
        )

        now = _utc_now()
        entry_id = hashlib.sha256(
            f"{self.run_id}:{provider}:{now.isoformat()}:{self._run_credits_used}".encode()
        ).hexdigest()[:16]

        with self._connect() as conn:
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
        domain: str | None = None,
        linkedin_url: str | None = None,
        person_name: str | None = None,
        email: str | None = None,
        force_refresh: bool = False,
        ttl: timedelta | None = None,
    ) -> tuple[EnrichmentResult | None, list[FallbackAttempt]]:
        """Check cache first; on miss, try providers in exact 1..N order with fallback on failure."""
        attempts: list[FallbackAttempt] = []

        if not force_refresh:
            cached = self.get(
                operation=operation,
                domain=domain,
                linkedin_url=linkedin_url,
                person_name=person_name,
                email=email,
            )
            if cached is not None:
                attempts.append(
                    FallbackAttempt(rank=0, provider=cached.source, status="hit", detail="cache_hit")
                )
                return cached, attempts

        usable_fn = is_usable_result or (
            lambda r: bool(r.company_patch or r.contacts or r.email_verified_status is not None)
        )

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
            attempts.append(
                FallbackAttempt(
                    rank=rank,
                    provider=provider,
                    status="hit",
                    detail=f"cached_key={cache_key[:12]}",
                )
            )
            return result, attempts

        return None, attempts

    def export_parquet(self, output_path: str | Path = "storage/enrichment_cache.parquet") -> Path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                f"COPY enrichment_cache TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return out

    def export_jsonl(self, output_path: str | Path = "storage/enrichment_cache.jsonl") -> Path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
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
    "ENV_PROVIDER_MAP",
    "ENV_PROVIDER_ORDER",
    "EnrichmentCache",
    "FallbackAttempt",
    "build_cache_key",
    "load_configured_providers",
    "normalize_linkedin_url",
    "slugify_person",
]