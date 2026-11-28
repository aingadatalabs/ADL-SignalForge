from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from enrichment import (
    EnrichmentCache,
    EnrichmentPolicy,
    LeadRecord,
    WaterfallRunReport,
    build_default_waterfall,
)
from enrichment.base import normalize_email, normalize_linkedin_url
from enrichment.providers.api_waterfall import Stage2ApiAdapter
from qualification.yc_gate import QualifiedEnrichmentCandidate, YCGatePolicy, YCPreEnrichmentGate
from yc_gtm_engine.config import ScraperConfig
from yc_gtm_engine.db import YCLeadsDatabase

logger = logging.getLogger("adl.signalforge.yc_gtm_engine.enrich_runner")

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_ARTIFACT_RE = re.compile(
    r"(?:\s*[,|–—-]\s*(?:co-?founder|founder|ceo|cto|coo|cpo|cmo|vp|head of|director|phd|md|mba|yc\s*[swfb]\d{2})\b.*$|\s*\([^)]*\)\s*$)",
    re.IGNORECASE,
)
_PLACEHOLDER_NAMES = {"", "unknown", "unknown contact", "n/a", "none"}

_EMAIL_STATUS_RANK: dict[str, int] = {
    "valid": 4,
    "verified": 4,
    "catch_all": 3,
    "catch-all": 3,
    "accept_all": 3,
    "unknown": 2,
    "risky": 2,
    "invalid": 1,
    "bounced": 1,
    "unverified": 0,
    "": 0,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_stage2_adapter(cache: EnrichmentCache) -> Stage2ApiAdapter:
    """
    Instantiate Stage2ApiAdapter defensively, passing cache if accepted by __init__
    while remaining compatible with zero-arg Stage2ApiAdapter() constructors.
    """
    try:
        sig = inspect.signature(Stage2ApiAdapter.__init__)
        params = [p for name, p in sig.parameters.items() if name != "self"]
        accepts_cache = "cache" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params
        )
        if accepts_cache:
            return Stage2ApiAdapter(cache=cache)
    except (TypeError, ValueError):
        pass
    return Stage2ApiAdapter()


async def _close_stage2_adapter(adapter: Stage2ApiAdapter | None) -> None:
    """Safely close Stage2ApiAdapter whether aclose() is async, sync, or absent."""
    if adapter is None:
        return
    aclose_fn = getattr(adapter, "aclose", None)
    if callable(aclose_fn):
        res = aclose_fn()
        if inspect.isawaitable(res):
            await res
        return
    close_fn = getattr(adapter, "close", None)
    if callable(close_fn):
        res = close_fn()
        if inspect.isawaitable(res):
            await res


def _normalize_verified_status(raw_status: str | None) -> str:
    val = str(raw_status or "").strip().lower()
    if val in {"verified", "valid"}:
        return "valid"
    if val in {"catch_all", "catch-all", "accept_all"}:
        return "catch_all"
    if val in {"unknown", "risky"}:
        return "unknown"
    if val in {"invalid", "bounced"}:
        return "invalid"
    return "unverified"


def _status_rank(status: str | None) -> int:
    return _EMAIL_STATUS_RANK.get(_normalize_verified_status(status), 0)


def _resolve_email_update(
    *,
    existing_email: str | None,
    existing_status: str | None,
    existing_conf: float | None,
    candidate_email: str | None,
    candidate_status: str | None,
    candidate_conf: float,
) -> tuple[str | None, str, float]:
    """
    Monotonically protect 'valid' email records from being downgraded by weaker
    statuses (catch_all, unknown, invalid, unverified) or overwritten by unverified/weaker emails.
    """
    r_email = normalize_email(existing_email)
    c_email = normalize_email(candidate_email)
    r_status = _normalize_verified_status(existing_status)
    c_status = _normalize_verified_status(candidate_status)
    r_rank = _status_rank(r_status)
    c_rank = _status_rank(c_status)
    r_conf = float(existing_conf or 0.0)
    c_conf = float(candidate_conf or 0.0)

    if not c_email:
        return r_email, r_status, r_conf

    if not r_email:
        return c_email, c_status, c_conf

    if r_email == c_email:
        if c_rank > r_rank:
            return r_email, c_status, max(r_conf, c_conf)
        if c_rank == r_rank:
            return r_email, r_status, max(r_conf, c_conf)
        # Never downgrade existing higher-rank status (e.g., valid -> catch_all/invalid/unknown)
        return r_email, r_status, r_conf

    # Different email addresses on the same founder row:
    # Never clobber an already-'valid' email with another address.
    if r_rank >= _EMAIL_STATUS_RANK["valid"]:
        return r_email, r_status, r_conf

    if c_rank > r_rank or (c_rank == r_rank and c_conf > r_conf):
        return c_email, c_status, c_conf

    return r_email, r_status, r_conf


def _clean_name(raw_name: str | None) -> str:
    if not raw_name:
        return ""
    cleaned = _WHITESPACE_RE.sub(" ", str(raw_name).strip())
    cleaned = _TRAILING_ARTIFACT_RE.sub("", cleaned).strip(" ,|-–—")
    return _WHITESPACE_RE.sub(" ", cleaned)


def _is_full_name(name: str) -> bool:
    """Return True only when a cleaned name has at least two whitespace-separated tokens."""
    cleaned = _clean_name(name)
    if cleaned.lower() in _PLACEHOLDER_NAMES:
        return False
    tokens = [t for t in cleaned.split(" ") if t]
    return len(tokens) >= 2


def _select_best_founder_name(existing_name: str | None, candidate_name: str | None) -> str:
    """
    Prefer a multi-token full name (contains whitespace) over a single-token first name,
    while refusing to overwrite an already-clean full name with longer noisy strings.
    """
    r_name = _clean_name(existing_name)
    c_name = _clean_name(candidate_name)

    r_is_placeholder = r_name.lower() in _PLACEHOLDER_NAMES
    c_is_placeholder = c_name.lower() in _PLACEHOLDER_NAMES

    if r_is_placeholder and not c_is_placeholder:
        return c_name
    if c_is_placeholder:
        return r_name

    r_has_space = _is_full_name(r_name)
    c_has_space = _is_full_name(c_name)

    if not r_has_space and c_has_space:
        return c_name

    return r_name


def _canonicalize_linkedin_url(raw_url: str | None) -> tuple[str | None, str | None]:
    """
    Return (canonical_https_url, canonical_match_key) standardized to
    https://www.linkedin.com/... when valid, or (None, None) if absent/invalid.
    """
    if not raw_url:
        return None, None

    normalized = normalize_linkedin_url(raw_url)
    candidate = (normalized or str(raw_url)).strip()
    if not candidate:
        return None, None

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = f"https://{candidate.lstrip('/')}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host.startswith("m."):
        host = host[2:]
    if host == "linkedin.com":
        host = "www.linkedin.com"

    path = re.sub(r"/+", "/", parsed.path or "").strip("/")
    if not host.endswith("linkedin.com") or not path:
        return None, None

    parts = path.split("/")
    if parts[0].lower() in {"in", "company", "school", "pub"} and len(parts) >= 2:
        slug = parts[1].strip("/").lower()
        if not slug:
            return None, None
        canonical_path = f"/{parts[0].lower()}/{slug}"
    else:
        canonical_path = f"/{path.lower()}"

    canonical_url = f"https://{host}{canonical_path}"
    match_key = f"{host}{canonical_path}"
    return canonical_url, match_key


def _match_existing_founder(
    existing_founders: list[dict[str, Any]],
    *,
    c_li_key: str | None,
    c_email: str | None,
    c_name: str,
) -> dict[str, Any] | None:
    """
    Deterministic contact matching order:
1. LinkedIn URL match (strongest identity)
2. Email match
3. Full-name match (requires >= 2 tokens on both sides to prevent
         first-name-only false positives such as 'Rishi' or 'David')
    """
    c_name_norm = _clean_name(c_name).lower()
    c_has_full_name = _is_full_name(c_name_norm)

    if c_li_key:
        for row in existing_founders:
            _, r_li_key = _canonicalize_linkedin_url(row.get("linkedin_url"))
            if r_li_key and c_li_key == r_li_key:
                return row

    if c_email:
        for row in existing_founders:
            r_email = normalize_email(row.get("email"))
            if r_email and c_email == r_email:
                return row

    if c_has_full_name:
        for row in existing_founders:
            r_name_norm = _clean_name(row.get("founder_name")).lower()
            if _is_full_name(r_name_norm) and c_name_norm == r_name_norm:
                return row

    return None


def ensure_enrichment_audit_schema(db: YCLeadsDatabase) -> None:
    """Ensure SQLite companies and founders tables have enrichment persistence columns."""
    with db.connect() as conn:
        company_cols = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "enrichment_status" not in company_cols:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'PENDING'"
            )
        if "enrichment_json" not in company_cols:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN enrichment_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "credits_used" not in company_cols:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN credits_used REAL NOT NULL DEFAULT 0.0"
            )
        if "last_enriched_at" not in company_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN last_enriched_at TEXT")

        founder_cols = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(founders)").fetchall()
        }
        if "email" not in founder_cols:
            conn.execute("ALTER TABLE founders ADD COLUMN email TEXT")
        if "email_verified_status" not in founder_cols:
            conn.execute(
                "ALTER TABLE founders ADD COLUMN email_verified_status TEXT NOT NULL DEFAULT 'unverified'"
            )
        if "email_confidence" not in founder_cols:
            conn.execute(
                "ALTER TABLE founders ADD COLUMN email_confidence REAL NOT NULL DEFAULT 0.0"
            )
        if "enrichment_source" not in founder_cols:
            conn.execute("ALTER TABLE founders ADD COLUMN enrichment_source TEXT")
        if "outreach_status" not in founder_cols:
            conn.execute(
                "ALTER TABLE founders ADD COLUMN outreach_status TEXT NOT NULL DEFAULT 'NEW'"
            )
        if "notes" not in founder_cols:
            conn.execute("ALTER TABLE founders ADD COLUMN notes TEXT")


def persist_enriched_candidate(
    db: YCLeadsDatabase,
    candidate: QualifiedEnrichmentCandidate,
    report: WaterfallRunReport,
) -> dict[str, Any]:
    """Write enriched contacts, company intelligence, and audit signals back into SQLite."""
    now = _utc_now_iso()
    lead: LeadRecord = report.lead
    credits_val = float(lead.credits_used or 0.0)

    with db.connect() as conn:
        company_cols = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        has_industry_col = "industry" in company_cols

        existing_founders = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM founders WHERE company_id = ?",
                (candidate.company_id,),
            ).fetchall()
        ]

        updated_contacts = 0
        inserted_contacts = 0

        for contact in lead.contacts:
            c_email = normalize_email(contact.email)
            c_li_url, c_li_key = _canonicalize_linkedin_url(contact.linkedin_url)
            c_name = _clean_name(contact.full_name)
            c_status = _normalize_verified_status(
                contact.verified_status.value if contact.verified_status else "unverified"
            )
            c_conf = float(contact.confidence or 0.0)
            c_src = (contact.source or "enrichment").strip()

            matched_row = _match_existing_founder(
                existing_founders,
                c_li_key=c_li_key,
                c_email=c_email,
                c_name=c_name,
            )

            if matched_row is not None:
                r_li_url, _ = _canonicalize_linkedin_url(matched_row.get("linkedin_url"))
                best_name = _select_best_founder_name(matched_row.get("founder_name"), c_name)
                best_li = c_li_url or r_li_url
                best_email, best_status, best_conf = _resolve_email_update(
                    existing_email=matched_row.get("email"),
                    existing_status=matched_row.get("email_verified_status"),
                    existing_conf=matched_row.get("email_confidence"),
                    candidate_email=c_email,
                    candidate_status=c_status,
                    candidate_conf=c_conf,
                )

                # Guard at both Python and SQLite levels so 'valid' can never be clobbered
                conn.execute(
                    """
                    UPDATE founders
                    SET founder_name = COALESCE(NULLIF(?, ''), founder_name),
                        role = COALESCE(NULLIF(?, ''), role),
                        linkedin_url = COALESCE(NULLIF(?, ''), linkedin_url),
                        email = COALESCE(NULLIF(?, ''), email),
                        email_verified_status = CASE
                            WHEN LOWER(COALESCE(email_verified_status, 'unverified')) IN ('valid', 'verified')
                                THEN 'valid'
                            ELSE ?
                        END,
                        email_confidence = CASE
                            WHEN ? > COALESCE(email_confidence, 0.0) THEN ?
                            ELSE COALESCE(email_confidence, 0.0)
                        END,
                        enrichment_source = COALESCE(NULLIF(?, ''), enrichment_source),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        best_name,
                        contact.title,
                        best_li,
                        best_email,
                        best_status,
                        best_conf,
                        best_conf,
                        c_src,
                        now,
                        int(matched_row["id"]),
                    ),
                )

                matched_row["founder_name"] = best_name or matched_row.get("founder_name")
                if best_li:
                    matched_row["linkedin_url"] = best_li
                if best_email:
                    matched_row["email"] = best_email
                matched_row["email_verified_status"] = (
                    "valid"
                    if _normalize_verified_status(matched_row.get("email_verified_status")) == "valid"
                    else best_status
                )
                matched_row["email_confidence"] = max(
                    float(matched_row.get("email_confidence") or 0.0),
                    best_conf,
                )
                updated_contacts += 1
            elif c_name or c_email:
                insert_name = c_name or (c_email.split("@", 1)[0] if c_email else "Unknown Contact")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO founders (
                        company_id, founder_name, role, bio, linkedin_url,
                        twitter_url, is_priority_buyer, outreach_status, notes,
                        created_at, updated_at, email, email_verified_status,
                        email_confidence, enrichment_source
                    )
                    VALUES (?, ?, ?, NULL, ?, NULL, 1, 'NEW', NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.company_id,
                        insert_name,
                        contact.title or "Decision Maker",
                        c_li_url,
                        now,
                        now,
                        c_email,
                        c_status,
                        c_conf,
                        c_src,
                    ),
                )
                existing_founders.append(
                    {
                        "company_id": candidate.company_id,
                        "founder_name": insert_name,
                        "linkedin_url": c_li_url,
                        "email": c_email,
                        "email_verified_status": c_status,
                        "email_confidence": c_conf,
                    }
                )
                inserted_contacts += 1

        enrichment_snapshot = {
            "industry": lead.industry,
            "company_size": lead.company_size,
            "location": lead.location,
            "extra": lead.extra,
            "stage_1_results": report.stage_1_results,
            "used_cache_or_api_fallback": report.used_cache_or_api_fallback,
            "stopped_early_zero_credits": report.stopped_early_zero_credits,
            "fallback_attempts": [
                {
                    "rank": a.rank,
                    "provider": a.provider,
                    "status": a.status,
                    "detail": a.detail,
                }
                for a in report.fallback_attempts
            ],
            "notes": report.notes,
        }

        if has_industry_col:
            conn.execute(
                """
                UPDATE companies
                SET industry = COALESCE(NULLIF(?, ''), industry),
                    enrichment_status = 'ENRICHED',
                    enrichment_json = ?,
                    credits_used = ROUND(COALESCE(credits_used, 0.0) + ?, 6),
                    last_enriched_at = ?
                WHERE id = ?
                """,
                (
                    lead.industry,
                    json.dumps(enrichment_snapshot, default=str),
                    credits_val,
                    now,
                    candidate.company_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE companies
                SET enrichment_status = 'ENRICHED',
                    enrichment_json = ?,
                    credits_used = ROUND(COALESCE(credits_used, 0.0) + ?, 6),
                    last_enriched_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(enrichment_snapshot, default=str),
                    credits_val,
                    now,
                    candidate.company_id,
                ),
            )

        signal_cols = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(company_signals)").fetchall()
        }
        has_detected_at = "detected_at" in signal_cols
        sig_title = (
            f"Enriched ({len(lead.contacts)} contacts, credits={credits_val:.2f}, "
            f"stage1={report.stage_1_results}, stage2={report.used_cache_or_api_fallback})"
        )

        existing_signal = conn.execute(
            """
            SELECT id
            FROM company_signals
            WHERE company_id = ? AND signal_type = 'ENRICHMENT_COMPLETED'
            ORDER BY id ASC
            LIMIT 1
            """,
            (candidate.company_id,),
        ).fetchone()

        if existing_signal is not None:
            if has_detected_at:
                conn.execute(
                    """
                    UPDATE company_signals
                    SET signal_title = ?, signal_url = NULL, detected_at = ?
                    WHERE id = ?
                    """,
                    (sig_title, now, int(existing_signal["id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE company_signals
                    SET signal_title = ?, signal_url = NULL
                    WHERE id = ?
                    """,
                    (sig_title, int(existing_signal["id"])),
                )
        elif has_detected_at:
            conn.execute(
                """
                INSERT OR IGNORE INTO company_signals (
                    company_id, signal_type, signal_title, signal_url, detected_at
                )
                VALUES (?, 'ENRICHMENT_COMPLETED', ?, NULL, ?)
                """,
                (candidate.company_id, sig_title, now),
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO company_signals (
                    company_id, signal_type, signal_title, signal_url
                )
                VALUES (?, 'ENRICHMENT_COMPLETED', ?, NULL)
                """,
                (candidate.company_id, sig_title),
            )

    return {
        "company_id": candidate.company_id,
        "slug": candidate.slug,
        "company_name": candidate.company_name,
        "domain": lead.domain,
        "credits_used": round(credits_val, 6),
        "stage_1_results": report.stage_1_results,
        "used_cache_or_api_fallback": report.used_cache_or_api_fallback,
        "stopped_early_zero_credits": report.stopped_early_zero_credits,
        "updated_contacts": updated_contacts,
        "inserted_contacts": inserted_contacts,
        "contacts": [
            {
                "full_name": _clean_name(c.full_name) or c.full_name,
                "title": c.title,
                "email": c.email,
                "verified_status": _normalize_verified_status(
                    c.verified_status.value if c.verified_status else "unverified"
                ),
                "linkedin_url": _canonicalize_linkedin_url(c.linkedin_url)[0] or c.linkedin_url,
                "source": c.source,
            }
            for c in lead.contacts
        ],
    }


async def run_yc_enrichment_async(
    *,
    limit: int | None = None,
    stage_1_only: bool = False,
    max_credits_per_lead: float = 2.0,
    max_run_credits: float = 10.0,
    concurrency: int = 3,
    force_refresh: bool = False,
) -> dict[str, Any]:
    cfg = ScraperConfig()
    db = YCLeadsDatabase(cfg.db_path)
    await asyncio.to_thread(ensure_enrichment_audit_schema, db)

    gate_policy = YCGatePolicy(
        default_enrichment_stage="stage_1" if stage_1_only else "waterfall",
        max_credits_per_lead=0.0 if stage_1_only else max_credits_per_lead,
        allow_stage_2_escalation=not stage_1_only,
    )
    gate = YCPreEnrichmentGate(config=cfg, policy=gate_policy)
    summary = await asyncio.to_thread(gate.evaluate_database, db, limit=limit, persist=True)

    if not summary.qualified_candidates:
        return {
            "qualified_candidates": 0,
            "enriched_leads": [],
            "cache_stats": {},
        }

    cache = EnrichmentCache(max_run_credits=max_run_credits)
    stage2_adapter = None if stage_1_only else _build_stage2_adapter(cache)
    waterfall = build_default_waterfall(
        cache=cache,
        stage2_caller=stage2_adapter,
    )

    semaphore = asyncio.Semaphore(max(1, concurrency))
    enriched_outputs: list[dict[str, Any]] = []

    async def _process_candidate(cand: QualifiedEnrichmentCandidate) -> dict[str, Any]:
        assert cand.lead_record is not None
        pol: EnrichmentPolicy = cand.enrichment_policy or gate.build_enrichment_policy()
        async with semaphore:
            report = await waterfall.enrich_lead(
                cand.lead_record,
                policy=pol,
                force_refresh=force_refresh,
            )
        return await asyncio.to_thread(persist_enriched_candidate, db, cand, report)

    try:
        tasks = [_process_candidate(cand) for cand in summary.qualified_candidates]
        enriched_outputs = list(await asyncio.gather(*tasks))
        cache_stats = cache.stats()
    finally:
        await _close_stage2_adapter(stage2_adapter)
        await asyncio.to_thread(cache.close)

    return {
        "evaluated": summary.total_evaluated,
        "qualified_candidates": summary.qualified_count,
        "stage_1_only": stage_1_only,
        "enriched_leads": enriched_outputs,
        "cache_stats": cache_stats,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich qualified YC leads via Stage 1 + Stage 2 waterfall.")
    parser.add_argument("--limit", type=int, default=15, help="Max companies to evaluate from SQLite.")
    parser.add_argument(
        "--stage1-only",
        action="store_true",
        help="Run Stage 1 only (zero API credits spent) as a dry run.",
    )
    parser.add_argument(
        "--max-credits-per-lead",
        type=float,
        default=2.0,
        help="Max Stage 2 credits per lead.",
    )
    parser.add_argument(
        "--max-run-credits",
        type=float,
        default=10.0,
        help="Global credit cap across the entire run.",
    )
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent lead enrichment tasks.")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass cache and force fresh lookups.")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    result = asyncio.run(
        run_yc_enrichment_async(
            limit=args.limit,
            stage_1_only=args.stage1_only,
            max_credits_per_lead=args.max_credits_per_lead,
            max_run_credits=args.max_run_credits,
            concurrency=args.concurrency,
            force_refresh=args.force_refresh,
        )
    )
    print(json.dumps(result, indent=2))