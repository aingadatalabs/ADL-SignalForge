from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class CompanyQualificationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    company_name: str | None = None
    canonical_domain: str | None = None
    website: str | None = None
    description: str | None = None
    services: tuple[str, ...] = Field(default_factory=tuple)
    country_code: str | None = None
    team_size_text: str | None = None
    is_unresolved_candidate: bool = False


RuleStatus = Literal["passed", "failed", "pending", "manual_review"]


class RuleCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    rule_id: str
    rule_name: str
    status: RuleStatus
    reason: str | None = None
    observed_value: Any | None = None
    expected_value: Any | None = None


class RuleEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    status: Literal["eligible", "disqualified", "pending", "manual_review"]
    checks: list[RuleCheck] = Field(default_factory=list)
    disqualification_reasons: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    fit_score: float = Field(description="Normalized 0-100 score for agency service fit")
    tech_score: float = Field(description="Normalized 0-100 score for tech stack alignment & opportunity")
    intent_access_score: float = Field(description="Normalized 0-100 score for intent signals & decision-maker access")
    composite_score: float = Field(description="Final weighted score between 0 and 100")
    evidence_coverage: float = Field(description="Percentage (0-100) indicating data completeness and trust")
    scoring_notes: list[str] = Field(default_factory=list)


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


class QualificationPipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    evaluation_id: str
    company_id: str
    canonical_domain: str
    orchestrator_version: str
    evaluated_at: str
    rule_result: RuleEvaluationResult
    score_breakdown: ScoreBreakdown | None = None
    tier_assignment: TierAssignmentResult
    total_duration_seconds: float
    stage_durations: dict[str, float] = Field(default_factory=dict)


class BatchItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    success: bool
    result: QualificationPipelineResult | None = None
    error_type: str | None = None
    error_message: str | None = None


class QualificationBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    batch_id: str
    total_submitted: int
    succeeded: int
    failed: int
    results: list[BatchItemResult]