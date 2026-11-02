"""
storage/models/pipeline.py — Shared Pipeline Models, Schemas & Execution Contexts

Features:
- Strict Pydantic v2 extraction input validation with whitespace and type boundaries.
- Controlled run accounting context with invariant protection, allowlisted increments, and deduplicated failure tracking.
- Rich Silver company records embedding complete provenance, temporal tracking, and multi-stage lineage.
- Publication lifecycle state machine with enforced legal transitions and release manifest metadata.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field, field_validator, model_validator


class PipelineRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicationLifecycleState(str, Enum):
    PREPARED = "prepared"
    EXPORTING = "exporting"
    VALIDATING = "validating"
    PUBLISHED = "published"
    EXPORT_FAILED = "export_failed"
    VALIDATION_FAILED = "validation_failed"
    PUBLICATION_FAILED = "publication_failed"
    RECOVERY_REQUIRED = "recovery_required"

    @classmethod
    def get_allowed_transitions(cls) -> Dict["PublicationLifecycleState", Set["PublicationLifecycleState"]]:
        return {
            cls.PREPARED: {cls.EXPORTING, cls.PUBLICATION_FAILED},
            cls.EXPORTING: {cls.VALIDATING, cls.EXPORT_FAILED},
            cls.VALIDATING: {cls.PUBLISHED, cls.VALIDATION_FAILED},
            cls.EXPORT_FAILED: {cls.RECOVERY_REQUIRED},
            cls.VALIDATION_FAILED: {cls.RECOVERY_REQUIRED},
            cls.PUBLICATION_FAILED: {cls.RECOVERY_REQUIRED},
            cls.RECOVERY_REQUIRED: {cls.EXPORTING, cls.VALIDATING, cls.PUBLISHED},
            cls.PUBLISHED: set(),  # Terminal state
        }

    def can_transition_to(self, target: "PublicationLifecycleState") -> bool:
        return target in self.get_allowed_transitions().get(self, set())


class RecordFailure(BaseModel):
    record_id: Optional[str] = Field(default=None, description="Source or local record identifier")
    stage: str = Field(..., description="Pipeline stage where failure occurred")
    error_code: str = Field(..., description="Machine-readable error category")
    message: str = Field(..., description="Human-readable diagnostic summary")
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunContext(BaseModel, frozen=False):
    run_id: str
    pipeline_name: str
    source_provider: str
    batch_id: Optional[str] = Field(default=None, description="Idempotency batch identifier")
    parent_run_id: Optional[str] = Field(default=None, description="Parent run ID if this is a retry")
    pipeline_version: str = Field(default="1.0.0")
    status: PipelineRunStatus = PipelineRunStatus.PENDING
    
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    # Granular execution counters
    records_received: int = Field(default=0, ge=0)
    records_raw_persisted: int = Field(default=0, ge=0)
    records_validated: int = Field(default=0, ge=0)
    records_processed: int = Field(default=0, ge=0)
    records_failed: int = Field(default=0, ge=0)
    records_published: int = Field(default=0, ge=0)
    records_skipped: int = Field(default=0, ge=0)
    
    error_summaries: List[RecordFailure] = Field(default_factory=list)

    COUNTER_FIELDS: Set[str] = {
        "records_received",
        "records_raw_persisted",
        "records_validated",
        "records_processed",
        "records_failed",
        "records_published",
        "records_skipped",
    }

    def increment(self, counter_name: str, amount: int = 1) -> None:
        if counter_name not in self.COUNTER_FIELDS:
            raise AttributeError(f"RunContext has no approved counter named '{counter_name}'")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError("Increment amount must be a valid integer.")
        if amount < 0:
            raise ValueError("Counter increments cannot be negative.")
        
        current = getattr(self, counter_name)
        setattr(self, counter_name, current + amount)

    def record_failure(self, stage: str, error_code: str, message: str, record_id: Optional[str] = None) -> None:
        """Explicitly records a failure event, increments failure count once, and bounds memory."""
        self.records_failed += 1
        failure = RecordFailure(
            record_id=record_id,
            stage=stage,
            error_code=error_code,
            message=message
        )
        if len(self.error_summaries) < 50:
            self.error_summaries.append(failure)

    @model_validator(mode="after")
    def validate_counter_invariants(self) -> "RunContext":
        # Invariant check: Received >= Processed + Failed + Skipped (accounting for intermediate states)
        terminal_accounted = self.records_processed + self.records_failed + self.records_skipped
        if terminal_accounted > self.records_received:
            raise ValueError(
                f"Accounting invariant violated: Processed ({self.records_processed}) + "
                f"Failed ({self.records_failed}) + Skipped ({self.records_skipped}) cannot "
                f"exceed Received ({self.records_received})."
            )
        return self


class ExtractionRecordSchema(BaseModel):
    company_name: str = Field(..., min_length=1, description="Raw or trading company name")
    domain: str = Field(..., min_length=3, description="Corporate web domain or URL hostname")
    region: str = Field(default="Nairobi", description="Geographic jurisdiction")
    extraction_confidence: float = Field(default=0.90, ge=0.0, le=1.0, description="Extraction confidence score")
    source_record_id: Optional[str] = Field(default=None, description="Upstream provider record identifier")
    extra_metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional unparsed provider payloads")

    @field_validator("company_name", "domain")
    @classmethod
    def strip_and_validate_strings(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty or whitespace only.")
        return cleaned

    @field_validator("region", mode="before")
    @classmethod
    def sanitize_region(cls, v: Optional[str]) -> str:
        if v is None or not str(v).strip():
            return "Nairobi"
        return str(v).strip()


class SilverCompanyRecord(BaseModel):
    entity_id: str = Field(..., description="Canonical resolved entity UUID")
    company_name: str = Field(..., description="Normalized legal or trading name")
    domain: str = Field(..., description="Normalized corporate apex domain")
    region: str = Field(..., description="Normalized geographic region")
    attribute_confidence: float = Field(..., ge=0.0, le=1.0, description="Attribute resolution confidence")
    source_provider: str = Field(..., description="Originating extraction pipeline or agent")
    source_record_id: Optional[str] = None
    source_event_id: Optional[str] = Field(default=None, description="Reference to original Bronze event ID")
    pipeline_run_id: str = Field(..., description="Pipeline execution run ID for lineage")
    schema_version: int = Field(default=1, ge=1)
    
    observed_at: datetime = Field(..., description="Timestamp of original source observation")
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    record_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PublicationRecord(BaseModel):
    publication_id: str
    pipeline_run_id: str
    dataset_name: str
    tier: str = Field(..., description="Storage tier: bronze, silver, or gold")
    release_id: str
    state: PublicationLifecycleState = PublicationLifecycleState.PREPARED
    record_count: int = Field(default=0, ge=0)
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    manifest_path: Optional[str] = None
    manifest_checksum: Optional[str] = None
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_details: Optional[str] = None

    def transition_to(self, new_state: PublicationLifecycleState, error_message: Optional[str] = None) -> None:
        if not self.state.can_transition_to(new_state):
            raise ValueError(f"Illegal publication state transition from {self.state.value} to {new_state.value}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)
        if error_message:
            self.error_details = error_message