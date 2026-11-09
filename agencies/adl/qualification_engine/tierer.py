import logging
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("signalforge.qualification.tierer")

QualificationOutcome = Literal[
    "eligible",
    "disqualified",
    "low_fit",
    "pending",
    "manual_review",
]

LeadTier = Literal[
    "tier_1_hot",
    "tier_2_nurture",
    "tier_3_low_fit",
    "not_tiered",
]

ActionDirective = Literal[
    "trigger_instant_alert_and_fastapi_webhook",
    "persist_to_duckdb_jsonl_sink_for_drip",
    "persist_to_duckdb_jsonl_sink_for_review",
    "archive_and_log_rejection",
    "archive_low_fit_score",
    "route_to_enrichment_pending_queue",
    "route_to_manual_review_queue",
    "none",
]


class TierAssignmentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    qualification_outcome: QualificationOutcome
    tier: LeadTier
    composite_score: float | None = None
    evidence_coverage: float | None = None
    action_directive: ActionDirective
    routing_reason: str
    routing_metadata: dict[str, Any] = Field(default_factory=dict)


class TiererConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    tier_1_threshold: float = Field(default=80.0, ge=0.0, le=100.0, description="Minimum score for Tier 1 Hot leads")
    tier_2_threshold: float = Field(default=50.0, ge=0.0, le=100.0, description="Minimum score for Tier 2 Nurture leads")
    min_evidence_coverage_for_tier1: float = Field(
        default=60.0, ge=0.0, le=100.0, description="Minimum data completeness percentage required for Tier 1"
    )

    @field_validator("tier_1_threshold")
    @classmethod
    def validate_thresholds(cls, v: float, info: Any) -> float:
        # Note: We validate tier_1 > tier_2 after initialization or via model validators if desired
        return v


class LeadTierer:
    def __init__(self, config: TiererConfig | None = None):
        self.config = config or TiererConfig()
        if self.config.tier_1_threshold <= self.config.tier_2_threshold:
            raise ValueError("tier_1_threshold must be strictly greater than tier_2_threshold")

    def tier(
        self,
        company_id: str,
        canonical_domain: str,
        rule_result: Any,
        score_breakdown: Any | None = None,
    ) -> TierAssignmentResult:
        """
        Translates structured hard-filter results and scoring breakdowns into an actionable GTM tier 
        without executing side-effects or outbound network requests.
        """
        rule_status = getattr(rule_result, "status", None)
        if not rule_status:
            raise ValueError(f"Invalid rule_result provided for company_id={company_id}: missing 'status' field.")

        disqualification_reasons = getattr(rule_result, "disqualification_reasons", []) or []
        checks = getattr(rule_result, "checks", []) or []

        routing_meta: dict[str, Any] = {
            "rule_status": rule_status,
            "disqualification_reasons": list(disqualification_reasons),
            "scoring_notes": list(getattr(score_breakdown, "scoring_notes", [])) if score_breakdown else [],
        }

        # 1. Handle Hard Eligibility Outcomes from rules.py
        if rule_status == "disqualified":
            return TierAssignmentResult(
                company_id=company_id,
                canonical_domain=canonical_domain,
                qualification_outcome="disqualified",
                tier="not_tiered",
                composite_score=None,
                evidence_coverage=None,
                action_directive="archive_and_log_rejection",
                routing_reason="Failed hard deterministic eligibility filters.",
                routing_metadata=routing_meta,
            )

        if rule_status == "pending":
            return TierAssignmentResult(
                company_id=company_id,
                canonical_domain=canonical_domain,
                qualification_outcome="pending",
                tier="not_tiered",
                composite_score=None,
                evidence_coverage=None,
                action_directive="route_to_enrichment_pending_queue",
                routing_metadata=routing_meta,
            )

        if rule_status == "manual_review":
            return TierAssignmentResult(
                company_id=company_id,
                canonical_domain=canonical_domain,
                qualification_outcome="manual_review",
                tier="not_tiered",
                composite_score=None,
                evidence_coverage=None,
                action_directive="route_to_manual_review_queue",
                routing_reason="Flagged for manual review due to missing or ambiguous hard filter criteria.",
                routing_metadata=routing_meta,
            )

        # 2. If eligible, score_breakdown is required
        if not score_breakdown:
            raise ValueError(f"Candidate {company_id} passed hard eligibility but lacks a score_breakdown.")

        composite_score = float(getattr(score_breakdown, "composite_score", 0.0))
        evidence_coverage = float(getattr(score_breakdown, "evidence_coverage", 0.0))

        # Validate score ranges
        if not (0.0 <= composite_score <= 100.0):
            raise ValueError(f"Composite score out of bounds [0, 100]: {composite_score}")
        if not (0.0 <= evidence_coverage <= 100.0):
            raise ValueError(f"Evidence coverage out of bounds [0, 100]: {evidence_coverage}")

        routing_meta["composite_score"] = composite_score
        routing_meta["evidence_coverage"] = evidence_coverage

        # 3. GTM Tier Assignment based on Composite Score & Evidence Thresholds
        if composite_score >= self.config.tier_1_threshold:
            if evidence_coverage >= self.config.min_evidence_coverage_for_tier1:
                return TierAssignmentResult(
                    company_id=company_id,
                    canonical_domain=canonical_domain,
                    qualification_outcome="eligible",
                    tier="tier_1_hot",
                    composite_score=composite_score,
                    evidence_coverage=evidence_coverage,
                    action_directive="trigger_instant_alert_and_fastapi_webhook",
                    routing_reason="Eligible with high commercial fit score and sufficient evidence coverage.",
                    routing_metadata=routing_meta,
                )
            else:
                # High numerical score, but sparse evidence coverage -> Downgrade to Nurture/Review
                routing_meta["downgrade_reason"] = (
                    f"Composite score ({composite_score}) met Tier 1, but evidence coverage "
                    f"({evidence_coverage}%) fell below required threshold ({self.config.min_evidence_coverage_for_tier1}%)."
                )
                logger.warning(
                    "Candidate downgraded from Tier 1 to Tier 2 due to low evidence coverage",
                    extra={"company_id": company_id, "evidence_coverage": evidence_coverage, "composite_score": composite_score}
                )
                return TierAssignmentResult(
                    company_id=company_id,
                    canonical_domain=canonical_domain,
                    qualification_outcome="eligible",
                    tier="tier_2_nurture",
                    composite_score=composite_score,
                    evidence_coverage=evidence_coverage,
                    action_directive="persist_to_duckdb_jsonl_sink_for_review",
                    routing_reason="High score matched Tier 1, but downgraded to Tier 2 due to low data confidence.",
                    routing_metadata=routing_meta,
                )

        elif composite_score >= self.config.tier_2_threshold:
            return TierAssignmentResult(
                company_id=company_id,
                canonical_domain=canonical_domain,
                qualification_outcome="eligible",
                tier="tier_2_nurture",
                composite_score=composite_score,
                evidence_coverage=evidence_coverage,
                action_directive="persist_to_duckdb_jsonl_sink_for_drip",
                routing_reason="Eligible with moderate commercial fit score.",
                routing_metadata=routing_meta,
            )

        else:
            return TierAssignmentResult(
                company_id=company_id,
                canonical_domain=canonical_domain,
                qualification_outcome="low_fit",
                tier="tier_3_low_fit",
                composite_score=composite_score,
                evidence_coverage=evidence_coverage,
                action_directive="archive_low_fit_score",
                routing_reason="Passed hard eligibility but scored below active nurture threshold.",
                routing_metadata=routing_meta,
            )