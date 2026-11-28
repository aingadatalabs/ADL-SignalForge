from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from enrichment.base import (
    ContactRecord,
    EnrichmentPolicy,
    LeadRecord,
    normalize_domain,
    normalize_linkedin_url,
)
from yc_gtm_engine.config import ScraperConfig
from yc_gtm_engine.db import YCLeadsDatabase


logger = logging.getLogger("adl.signalforge.qualification.yc_gate")

_DEFAULT_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        "ycombinator.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "crunchbase.com",
        "facebook.com",
        "instagram.com",
        "github.com",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "medium.com",
        "substack.com",
        "wellfound.com",
        "angel.co",
        "producthunt.com",
        "g2.com",
        "capterra.com",
        "clutch.co",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_qualification_audit_schema(db: YCLeadsDatabase) -> None:
    """Ensure SQLite companies table has qualification_status and gated_reason columns."""
    with db.connect() as conn:
        cols = {
            str(row["name"]).lower()
            for row in conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "qualification_status" not in cols:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN qualification_status TEXT NOT NULL DEFAULT 'PENDING'"
            )
        if "gated_reason" not in cols:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN gated_reason TEXT"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_companies_qualification_status
                ON companies (qualification_status, icp_tier, icp_score DESC)
            """
        )


@dataclass(frozen=True)
class YCGatePolicy:
    """Credit-gating policy before running enrichment waterfall providers."""

    allowed_tiers: tuple[str, ...] = ("TIER_1_HOT", "TIER_2_WARM")
    min_icp_score: int = 55
    require_domain: bool = True
    require_priority_buyer: bool = True
    require_hiring_or_signal: bool = False
    blocked_domains: frozenset[str] = _DEFAULT_BLOCKED_DOMAINS
    default_enrichment_stage: str = "waterfall"
    max_credits_per_lead: float = 10.0
    min_enrichment_confidence: float = 0.6
    verify_emails: bool = True
    allow_stage_2_escalation: bool = True
    cache_ttl_seconds: int = 86_400


@dataclass
class QualifiedEnrichmentCandidate:
    """Payload ready for downstream enrichment waterfall or qualification routing."""

    company_id: int
    slug: str
    company_name: str
    batch: str
    domain: str | None
    yc_url: str
    icp_score: int
    icp_tier: str
    is_hiring: bool
    signal_count: int
    priority_buyers: list[dict[str, Any]] = field(default_factory=list)
    all_founders: list[dict[str, Any]] = field(default_factory=list)
    lead_record: LeadRecord | None = None
    enrichment_policy: EnrichmentPolicy | None = None


@dataclass
class GatedLeadDecision:
    """Outcome of evaluating a single scraped YC company for enrichment eligibility."""

    company_id: int
    slug: str
    company_name: str
    domain: str | None
    icp_score: int
    icp_tier: str
    qualified: bool
    reasons: list[str] = field(default_factory=list)
    candidate: QualifiedEnrichmentCandidate | None = None
    qualification_status: str = "GATED_OUT"
    gated_reason: str | None = None
    skipped_prior_evaluation: bool = False


@dataclass
class GateBatchSummary:
    """Aggregate summary of a pre-enrichment qualification pass."""

    total_evaluated: int
    qualified_count: int
    gated_out_count: int
    reason_breakdown: dict[str, int]
    qualified_candidates: list[QualifiedEnrichmentCandidate]
    decisions: list[GatedLeadDecision]
    skipped_prior_count: int = 0


class YCPreEnrichmentGate:
    """
    Filters and scores scraped YC companies & founders from SQLite so only
    qualified ICP-aligned leads with priority buyers enter enrichment.
    """

    def __init__(
        self,
        config: ScraperConfig | None = None,
        policy: YCGatePolicy | None = None,
    ) -> None:
        self.config = config or ScraperConfig()
        self.policy = policy or YCGatePolicy()

    def build_enrichment_policy(self) -> EnrichmentPolicy:
        """Build an EnrichmentPolicy aligned with ScraperConfig.target_buyer_roles."""
        target_titles = [
            role.strip()
            for role in (self.config.target_buyer_roles or ())
            if role and role.strip()
        ]
        stage_val = self.policy.default_enrichment_stage
        if stage_val not in {"stage_1", "stage_2", "waterfall"}:
            stage_val = "waterfall"
        return EnrichmentPolicy(
            stage=stage_val,  # type: ignore[arg-type]
            max_credits_per_lead=self.policy.max_credits_per_lead,
            min_confidence=self.policy.min_enrichment_confidence,
            target_titles=target_titles,
            verify_emails=self.policy.verify_emails,
            allow_stage_2_escalation=self.policy.allow_stage_2_escalation,
            cache_ttl_seconds=self.policy.cache_ttl_seconds,
        )

    def _is_blocked_domain(self, domain: str | None) -> bool:
        if not domain:
            return False
        clean = domain.lower().strip().removeprefix("www.")
        if clean in self.policy.blocked_domains:
            return True
        return any(clean.endswith(f".{blocked}") for blocked in self.policy.blocked_domains)

    @staticmethod
    def _split_name(full_name: str) -> tuple[str | None, str | None]:
        parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
        if not parts:
            return None, None
        if len(parts) == 1:
            return parts[0], None
        return parts[0], " ".join(parts[1:])

    def _build_lead_record(
        self,
        company_row: dict[str, Any],
        domain: str | None,
        founders: list[dict[str, Any]],
    ) -> LeadRecord:
        contacts: list[ContactRecord] = []
        for f in founders:
            raw_name = str(f.get("founder_name") or "").strip()
            first_name, last_name = self._split_name(raw_name)
            role = str(f.get("role") or "Founder").strip() or "Founder"
            li_norm = normalize_linkedin_url(f.get("linkedin_url"))
            li_url = f"https://{li_norm}" if li_norm else None
            is_prio = bool(f.get("is_priority_buyer"))
            contacts.append(
                ContactRecord(
                    full_name=raw_name or None,
                    first_name=first_name,
                    last_name=last_name,
                    title=role,
                    linkedin_url=li_url,
                    confidence=0.85 if is_prio else 0.55,
                    source="yc_directory",
                )
            )

        return LeadRecord(
            company_name=str(company_row.get("company_name") or "").strip() or None,
            domain=domain,
            website=company_row.get("website_url"),
            contacts=contacts,
        )

    def evaluate_row(
        self,
        company_row: dict[str, Any],
        founders: list[dict[str, Any]],
        signal_count: int = 0,
    ) -> GatedLeadDecision:
        company_id = int(company_row["id"])
        slug = str(company_row.get("slug") or "").strip().lower()
        company_name = str(company_row.get("company_name") or slug).strip()
        yc_url = str(company_row.get("yc_url") or f"https://www.ycombinator.com/companies/{slug}").strip()
        batch = str(company_row.get("batch") or "UNKNOWN").strip().upper()
        icp_score = int(company_row.get("icp_score") or 0)
        icp_tier = str(company_row.get("icp_tier") or "TIER_3_MONITOR").strip()
        is_hiring = bool(company_row.get("is_hiring", 0))
        one_liner = str(company_row.get("one_liner") or "").strip()

        raw_verticals = company_row.get("verticals_json")
        if isinstance(raw_verticals, str):
            try:
                parsed_v = json.loads(raw_verticals)
                verticals = [str(v) for v in parsed_v] if isinstance(parsed_v, list) else []
            except json.JSONDecodeError:
                verticals = []
        elif isinstance(raw_verticals, list):
            verticals = [str(v) for v in raw_verticals]
        else:
            verticals = []

        domain = normalize_domain(company_row.get("website_url"))
        reasons: list[str] = []

        excluded, excl_reason = self.config.is_excluded(tags=verticals, text=one_liner)
        if excluded:
            reasons.append(f"excluded:{excl_reason or 'policy'}")

        if self.policy.require_domain and not domain:
            reasons.append("missing_domain")
        elif self._is_blocked_domain(domain):
            reasons.append(f"blocked_domain:{domain}")

        if icp_tier not in self.policy.allowed_tiers:
            reasons.append(f"tier_not_allowed:{icp_tier}")

        if icp_score < self.policy.min_icp_score:
            reasons.append(f"icp_score_below_min:{icp_score}<{self.policy.min_icp_score}")

        if self.policy.require_hiring_or_signal and not (is_hiring or signal_count > 0):
            reasons.append("missing_hiring_or_signal")

        priority_buyers = [
            f
            for f in founders
            if bool(f.get("is_priority_buyer"))
            or self.config.score_buyer_role(str(f.get("role") or "")) >= self.config.min_buyer_role_score
        ]
        if self.policy.require_priority_buyer and not priority_buyers:
            reasons.append("missing_priority_buyer")

        qualified = len(reasons) == 0
        qual_status = "QUALIFIED" if qualified else "GATED_OUT"
        gated_reason_str = None if qualified else "; ".join(reasons)
        candidate: QualifiedEnrichmentCandidate | None = None

        if qualified:
            lead_rec = self._build_lead_record(
                company_row=company_row,
                domain=domain,
                founders=priority_buyers if priority_buyers else founders,
            )
            enrichment_pol = self.build_enrichment_policy()
            candidate = QualifiedEnrichmentCandidate(
                company_id=company_id,
                slug=slug,
                company_name=company_name,
                batch=batch,
                domain=domain,
                yc_url=yc_url,
                icp_score=icp_score,
                icp_tier=icp_tier,
                is_hiring=is_hiring,
                signal_count=signal_count,
                priority_buyers=priority_buyers,
                all_founders=founders,
                lead_record=lead_rec,
                enrichment_policy=enrichment_pol,
            )

        return GatedLeadDecision(
            company_id=company_id,
            slug=slug,
            company_name=company_name,
            domain=domain,
            icp_score=icp_score,
            icp_tier=icp_tier,
            qualified=qualified,
            reasons=reasons,
            candidate=candidate,
            qualification_status=qual_status,
            gated_reason=gated_reason_str,
            skipped_prior_evaluation=False,
        )

    def _persist_qualification_audit(
        self,
        db: YCLeadsDatabase,
        decisions: list[GatedLeadDecision],
        *,
        record_signals: bool = True,
    ) -> None:
        now = _utc_now_iso()
        with db.connect() as conn:
            signal_cols = {
                str(r["name"]).lower()
                for r in conn.execute("PRAGMA table_info(company_signals)").fetchall()
            }
            if record_signals and "detected_at" not in signal_cols:
                conn.execute("ALTER TABLE company_signals ADD COLUMN detected_at TEXT")
                signal_cols.add("detected_at")

            for dec in decisions:
                if dec.skipped_prior_evaluation:
                    continue
                conn.execute(
                    """
                    UPDATE companies
                    SET qualification_status = ?, gated_reason = ?
                    WHERE id = ?
                    """,
                    (dec.qualification_status, dec.gated_reason, dec.company_id),
                )
                if record_signals:
                    sig_type = (
                        "PRE_ENRICHMENT_QUALIFIED"
                        if dec.qualified
                        else "PRE_ENRICHMENT_GATED"
                    )
                    sig_title = (
                        f"Qualified for enrichment ({dec.icp_tier}, score={dec.icp_score})"
                        if dec.qualified
                        else f"Gated out: {dec.gated_reason or 'gated_out'}"
                    )
                    if "detected_at" in signal_cols:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO company_signals (
                                company_id, signal_type, signal_title, signal_url, detected_at
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (dec.company_id, sig_type, sig_title, None, now),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO company_signals (
                                company_id, signal_type, signal_title, signal_url
                            )
                            VALUES (?, ?, ?, ?)
                            """,
                            (dec.company_id, sig_type, sig_title, None),
                        )

    def evaluate_database(
        self,
        db: YCLeadsDatabase,
        *,
        batches: tuple[str, ...] | None = None,
        limit: int | None = None,
        persist: bool = True,
        record_signals: bool = True,
        skip_already_evaluated: bool = False,
    ) -> GateBatchSummary:
        """Load companies, founders, and signal counts from SQLite and apply the pre-enrichment gate."""
        ensure_qualification_audit_schema(db)

        query = "SELECT * FROM companies"
        params: list[Any] = []
        where_clauses: list[str] = []

        if batches:
            clean_batches = [b.strip().upper() for b in batches if b and b.strip()]
            if clean_batches:
                placeholders = ", ".join("?" for _ in clean_batches)
                where_clauses.append(f"UPPER(batch) IN ({placeholders})")
                params.extend(clean_batches)

        if skip_already_evaluated:
            where_clauses.append(
                "(qualification_status IN ('PENDING', 'UNSCORED') OR qualification_status IS NULL)"
            )

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        query += " ORDER BY icp_score DESC, id ASC"
        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be >= 0, got {limit}")
            query += " LIMIT ?"
            params.append(limit)

        founders_by_company: dict[int, list[dict[str, Any]]] = {}
        signals_by_company: dict[int, int] = {}

        with db.connect() as conn:
            company_rows = [dict(r) for r in conn.execute(query, params).fetchall()]
            for f_row in conn.execute("SELECT * FROM founders ORDER BY is_priority_buyer DESC, id ASC").fetchall():
                d = dict(f_row)
                founders_by_company.setdefault(int(d["company_id"]), []).append(d)
            for s_row in conn.execute(
                "SELECT company_id, COUNT(*) AS cnt FROM company_signals GROUP BY company_id"
            ).fetchall():
                signals_by_company[int(s_row["company_id"])] = int(s_row["cnt"])

        decisions: list[GatedLeadDecision] = []
        qualified_candidates: list[QualifiedEnrichmentCandidate] = []
        reason_breakdown: dict[str, int] = {}
        skipped_prior_count = 0

        for row in company_rows:
            cid = int(row["id"])
            existing_status = str(row.get("qualification_status") or "PENDING").strip().upper()
            if skip_already_evaluated and existing_status in {"QUALIFIED", "GATED_OUT"}:
                skipped_prior_count += 1
                existing_reason = row.get("gated_reason")
                prior_reasons = (
                    [r.strip() for r in str(existing_reason).split(";") if r.strip()]
                    if existing_reason
                    else []
                )
                is_qual = existing_status == "QUALIFIED"
                cand: QualifiedEnrichmentCandidate | None = None
                if is_qual:
                    dom = normalize_domain(row.get("website_url"))
                    f_list = founders_by_company.get(cid, [])
                    prio = [
                        f
                        for f in f_list
                        if bool(f.get("is_priority_buyer"))
                        or self.config.score_buyer_role(str(f.get("role") or "")) >= self.config.min_buyer_role_score
                    ]
                    cand = QualifiedEnrichmentCandidate(
                        company_id=cid,
                        slug=str(row.get("slug") or "").strip().lower(),
                        company_name=str(row.get("company_name") or "").strip(),
                        batch=str(row.get("batch") or "UNKNOWN").strip().upper(),
                        domain=dom,
                        yc_url=str(row.get("yc_url") or ""),
                        icp_score=int(row.get("icp_score") or 0),
                        icp_tier=str(row.get("icp_tier") or "TIER_3_MONITOR").strip(),
                        is_hiring=bool(row.get("is_hiring", 0)),
                        signal_count=signals_by_company.get(cid, 0),
                        priority_buyers=prio,
                        all_founders=f_list,
                        lead_record=self._build_lead_record(row, dom, prio if prio else f_list),
                        enrichment_policy=self.build_enrichment_policy(),
                    )
                    qualified_candidates.append(cand)
                else:
                    for reason in prior_reasons:
                        reason_breakdown[reason] = reason_breakdown.get(reason, 0) + 1

                decisions.append(
                    GatedLeadDecision(
                        company_id=cid,
                        slug=str(row.get("slug") or "").strip().lower(),
                        company_name=str(row.get("company_name") or "").strip(),
                        domain=normalize_domain(row.get("website_url")),
                        icp_score=int(row.get("icp_score") or 0),
                        icp_tier=str(row.get("icp_tier") or "TIER_3_MONITOR").strip(),
                        qualified=is_qual,
                        reasons=prior_reasons,
                        candidate=cand,
                        qualification_status=existing_status,
                        gated_reason=str(existing_reason) if existing_reason else None,
                        skipped_prior_evaluation=True,
                    )
                )
                continue

            decision = self.evaluate_row(
                company_row=row,
                founders=founders_by_company.get(cid, []),
                signal_count=signals_by_company.get(cid, 0),
            )
            decisions.append(decision)
            if decision.qualified and decision.candidate is not None:
                qualified_candidates.append(decision.candidate)
            else:
                for reason in decision.reasons:
                    reason_breakdown[reason] = reason_breakdown.get(reason, 0) + 1

        if persist and decisions:
            self._persist_qualification_audit(
                db,
                decisions,
                record_signals=record_signals,
            )

        return GateBatchSummary(
            total_evaluated=len(decisions),
            qualified_count=len(qualified_candidates),
            gated_out_count=len(decisions) - len(qualified_candidates),
            reason_breakdown=reason_breakdown,
            qualified_candidates=qualified_candidates,
            decisions=decisions,
            skipped_prior_count=skipped_prior_count,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-enrichment credit gate for YC leads.")
    parser.add_argument("--limit", type=int, default=None, help="Max companies to evaluate.")
    parser.add_argument(
        "--allow-missing-buyer",
        action="store_true",
        help="Allow companies without a priority buyer contact to pass the gate.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write qualification_status, gated_reason, or audit signals to SQLite.",
    )
    parser.add_argument(
        "--skip-already-evaluated",
        action="store_true",
        help="Skip companies already marked QUALIFIED or GATED_OUT in SQLite.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    cfg = ScraperConfig()
    database = YCLeadsDatabase(cfg.db_path)
    gate = YCPreEnrichmentGate(
        config=cfg,
        policy=YCGatePolicy(require_priority_buyer=not args.allow_missing_buyer),
    )
    summary = gate.evaluate_database(
        database,
        limit=args.limit,
        persist=not args.no_persist,
        skip_already_evaluated=args.skip_already_evaluated,
    )
    output = {
        "total_evaluated": summary.total_evaluated,
        "qualified_count": summary.qualified_count,
        "gated_out_count": summary.gated_out_count,
        "skipped_prior_count": summary.skipped_prior_count,
        "reason_breakdown": summary.reason_breakdown,
        "target_titles_applied": gate.build_enrichment_policy().target_titles,
        "qualified_leads": [
            {
                "company_id": c.company_id,
                "slug": c.slug,
                "company_name": c.company_name,
                "batch": c.batch,
                "domain": c.domain,
                "icp_score": c.icp_score,
                "icp_tier": c.icp_tier,
                "priority_buyers": [
                    {
                        "name": b.get("founder_name"),
                        "role": b.get("role"),
                        "linkedin_url": b.get("linkedin_url"),
                    }
                    for b in c.priority_buyers
                ],
            }
            for c in summary.qualified_candidates
        ],
    }
    print(json.dumps(output, indent=2))