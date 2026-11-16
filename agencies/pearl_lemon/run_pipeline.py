"""Entry point and master orchestrator for the Pearl Lemon agency pipeline."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agencies.adl.storage.jsonl_repository import JSONLRepository
from agencies.pearl_lemon.dedup import (
    DEFAULT_RESOLVED_RECORDS_PATH,
    PearlLemonDeduplicator,
)
from agencies.pearl_lemon.enrichment import (
    DEFAULT_ENRICHED_RECORDS_PATH,
    run_enrichment,
)
from agencies.pearl_lemon.extraction.extraction import (
    DEFAULT_RAW_RECORDS_PATH,
    ExtractionRunError,
    run_extraction,
)
from agencies.pearl_lemon.icp import DEFAULT_PEARL_LEMON_ICP, PearlLemonICP
from agencies.pearl_lemon.normalizer import (
    DEFAULT_NORMALIZED_RECORDS_PATH,
    PearlLemonNormalizer,
)
from agencies.pearl_lemon.qualification import (
    DEFAULT_QUALIFIED_LEADS_PATH,
    PearlLemonQualificationSettings,
    run_qualification,
)

logger = logging.getLogger(__name__)

PEARL_LEMON_PIPELINE_VERSION = "1.0.0"
EXPECTED_STAGE_ERRORS = (OSError, RuntimeError, TypeError, ValueError)


@dataclass(frozen=True)
class PearlLemonPipelineConfig:
    max_results_per_query: int = 10
    max_concurrency: int = 4
    similarity_threshold: float = 0.85
    raw_output_path: Path = field(default_factory=lambda: DEFAULT_RAW_RECORDS_PATH)
    normalized_output_path: Path = field(
        default_factory=lambda: DEFAULT_NORMALIZED_RECORDS_PATH
    )
    resolved_output_path: Path = field(
        default_factory=lambda: DEFAULT_RESOLVED_RECORDS_PATH
    )
    enriched_output_path: Path = field(
        default_factory=lambda: DEFAULT_ENRICHED_RECORDS_PATH
    )
    qualified_output_path: Path = field(
        default_factory=lambda: DEFAULT_QUALIFIED_LEADS_PATH
    )

    def __post_init__(self) -> None:
        if self.max_results_per_query < 1:
            raise ValueError("max_results_per_query must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if not 0 <= self.similarity_threshold <= 1:
            raise ValueError("similarity_threshold must be between 0 and 1")
        for name in (
            "raw_output_path",
            "normalized_output_path",
            "resolved_output_path",
            "enriched_output_path",
            "qualified_output_path",
        ):
            object.__setattr__(self, name, Path(getattr(self, name)))


class PipelineExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    agency: Literal["pearl_lemon"] = "pearl_lemon"
    started_at: datetime
    pipeline_version: str


class PipelineMetrics(BaseModel):
    discovered: int = 0
    normalized: int = 0
    identity_resolved: int = 0
    deduplicated: int = 0
    duplicates_removed: int = 0
    enriched: int = 0
    enrichment_partial: int = 0
    enrichment_failed: int = 0
    qualified: int = 0
    pending: int = 0
    disqualified: int = 0


class PipelineError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    error_type: str
    message: str
    company_id: str | None = None


class PipelineOutputs(BaseModel):
    raw_records: str
    normalized_records: str
    resolved_records: str
    enriched_records: str
    qualified_leads: str


class PipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context: PipelineExecutionContext
    status: Literal["completed", "partial", "failed"]
    metrics: PipelineMetrics
    errors: list[PipelineError] = Field(
        default_factory=lambda: list[PipelineError]()
    )
    outputs: PipelineOutputs
    finished_at: datetime
    duration_seconds: float = Field(ge=0)


async def run_pipeline(
    config: PearlLemonPipelineConfig | None = None,
    *,
    icp: PearlLemonICP | None = None,
    qualification_settings: PearlLemonQualificationSettings | None = None,
    frankensurf_client: Any | None = None,
    hunter_api_key: str | None = None,
    http_client: Any | None = None,
    dns_resolver: Any | None = None,
) -> PipelineResult:
    """Run extraction through qualification and return an auditable run summary."""
    pipeline_config = config or PearlLemonPipelineConfig()
    if qualification_settings is not None:
        if icp is not None and qualification_settings.eligibility != icp:
            raise ValueError(
                "The extraction ICP and qualification eligibility ICP must match"
            )
        active_icp = qualification_settings.eligibility
        active_qualification_settings = qualification_settings
    else:
        active_icp = icp or DEFAULT_PEARL_LEMON_ICP
        active_qualification_settings = PearlLemonQualificationSettings(
            eligibility=active_icp
        )

    started_at = datetime.now(timezone.utc)
    started_clock = perf_counter()
    context = PipelineExecutionContext(
        run_id=f"pl_{started_at:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}",
        started_at=started_at,
        pipeline_version=PEARL_LEMON_PIPELINE_VERSION,
    )
    serialized_context = context.model_dump(mode="json")
    metrics = PipelineMetrics()
    errors: list[PipelineError] = list[PipelineError]()
    outputs = PipelineOutputs(
        raw_records=str(pipeline_config.raw_output_path),
        normalized_records=str(pipeline_config.normalized_output_path),
        resolved_records=str(pipeline_config.resolved_output_path),
        enriched_records=str(pipeline_config.enriched_output_path),
        qualified_leads=str(pipeline_config.qualified_output_path),
    )
    extraction_was_partial = False

    def make_result(status: Literal["completed", "partial", "failed"]) -> PipelineResult:
        return PipelineResult(
            context=context,
            status=status,
            metrics=metrics,
            errors=errors,
            outputs=outputs,
            finished_at=datetime.now(timezone.utc),
            duration_seconds=round(perf_counter() - started_clock, 4),
        )

    def record_stage_failure(stage: str, error: Exception) -> PipelineResult:
        logger.exception(
            "Pearl Lemon pipeline stage failed",
            extra={"stage": stage, "run_id": context.run_id},
        )
        errors.append(
            PipelineError(
                stage=stage,
                error_type=type(error).__name__,
                message=str(error),
            )
        )
        has_progress = metrics.discovered > 0 or metrics.normalized > 0
        return make_result("partial" if has_progress else "failed")

    logger.info(
        "Starting Pearl Lemon pipeline",
        extra={"run_id": context.run_id, "pipeline_version": context.pipeline_version},
    )

    logger.info("Starting extraction stage", extra={"run_id": context.run_id})
    try:
        raw_records = await run_extraction(
            max_results_per_query=pipeline_config.max_results_per_query,
            icp=active_icp,
            client=frankensurf_client,
            output_path=pipeline_config.raw_output_path,
            pipeline_context=serialized_context,
        )
    except ExtractionRunError as exc:
        raw_records = exc.partial_results
        errors.append(
            PipelineError(
                stage="extraction",
                error_type=type(exc).__name__,
                message=str(exc),
            )
        )
        extraction_was_partial = True
        if not raw_records:
            return make_result("failed")
        logger.warning(
            "Continuing with partial extraction results",
            extra={"run_id": context.run_id, "count": len(raw_records)},
        )
    except EXPECTED_STAGE_ERRORS as exc:
        return record_stage_failure("extraction", exc)
    metrics.discovered = len(raw_records)

    logger.info("Starting normalization stage", extra={"run_id": context.run_id})
    try:
        normalizer = PearlLemonNormalizer(
            raw_agencies=raw_records,
            input_path=pipeline_config.raw_output_path,
            output_path=pipeline_config.normalized_output_path,
        )
        normalizer.process()
        normalized_records = normalizer.normalized_records
        metrics.normalized = len(normalized_records)
    except EXPECTED_STAGE_ERRORS as exc:
        return record_stage_failure("normalization", exc)

    logger.info(
        "Starting identity resolution and deduplication stage",
        extra={"run_id": context.run_id},
    )
    try:
        deduplicator = PearlLemonDeduplicator(
            similarity_threshold=pipeline_config.similarity_threshold,
            normalized_records=normalized_records,
            input_path=pipeline_config.normalized_output_path,
            output_path=pipeline_config.resolved_output_path,
            pipeline_context=serialized_context,
        )
        resolved_records = deduplicator.process()
        metrics.deduplicated = len(resolved_records)
        metrics.duplicates_removed = max(
            0, metrics.normalized - metrics.deduplicated
        )
        metrics.identity_resolved = sum(
            bool(record.get("entity_id"))
            and record.get("entity_id") != "unknown"
            for record in resolved_records
        )
    except EXPECTED_STAGE_ERRORS as exc:
        return record_stage_failure("identity_resolution", exc)

    logger.info("Starting enrichment stage", extra={"run_id": context.run_id})
    try:
        if resolved_records:
            enriched_records = await run_enrichment(
                input_path=pipeline_config.resolved_output_path,
                output_path=pipeline_config.enriched_output_path,
                records=resolved_records,
                icp=active_icp,
                frankensurf_client=frankensurf_client,
                hunter_api_key=hunter_api_key,
                http_client=http_client,
                dns_resolver=dns_resolver,
                max_concurrency=pipeline_config.max_concurrency,
            )
        else:
            JSONLRepository(
                pipeline_config.enriched_output_path,
                enable_locking=False,
            ).overwrite(())
            enriched_records = []
        metrics.enriched = len(enriched_records)
        metrics.enrichment_partial = sum(
            record.get("enrichment_status") == "partial"
            for record in enriched_records
        )
        metrics.enrichment_failed = sum(
            record.get("enrichment_status") == "failed"
            for record in enriched_records
        )
        for record in enriched_records:
            provider_errors = record.get("enrichment_errors", [])
            if isinstance(provider_errors, list):
                for provider_error in cast(list[Any], provider_errors):
                    if not isinstance(provider_error, Mapping):
                        continue
                    error_data = cast(Mapping[str, Any], provider_error)
                    provider_name = error_data.get("provider")
                    provider_message = error_data.get("error")
                    errors.append(
                        PipelineError(
                            stage="enrichment",
                            error_type=(
                                provider_name
                                if isinstance(provider_name, str)
                                else "provider_error"
                            ),
                            message=(
                                provider_message
                                if isinstance(provider_message, str)
                                else "Provider enrichment failed"
                            ),
                            company_id=str(
                                record.get("company_id")
                                or record.get("entity_id")
                                or ""
                            )
                            or None,
                        )
                    )
    except EXPECTED_STAGE_ERRORS as exc:
        return record_stage_failure("enrichment", exc)

    logger.info("Starting qualification stage", extra={"run_id": context.run_id})
    try:
        qualified_results = await run_qualification(
            input_path=pipeline_config.enriched_output_path,
            output_path=pipeline_config.qualified_output_path,
            settings=active_qualification_settings,
        )
        metrics.qualified = sum(
            result.qualification.status == "eligible"
            for result in qualified_results
        )
        metrics.pending = sum(
            result.qualification.status == "pending"
            for result in qualified_results
        )
        metrics.disqualified = sum(
            result.qualification.status == "disqualified"
            for result in qualified_results
        )
    except EXPECTED_STAGE_ERRORS as exc:
        return record_stage_failure("qualification", exc)

    status: Literal["completed", "partial", "failed"] = (
        "partial"
        if extraction_was_partial
        or metrics.enrichment_partial
        or metrics.enrichment_failed
        else "completed"
    )
    logger.info(
        "Pearl Lemon pipeline finished",
        extra={
            "run_id": context.run_id,
            "status": status,
            "discovered": metrics.discovered,
            "qualified": metrics.qualified,
        },
    )
    return make_result(status)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_pipeline())
    if result.status != "completed":
        raise SystemExit(1)


__all__ = [
    "PEARL_LEMON_PIPELINE_VERSION",
    "PearlLemonPipelineConfig",
    "PipelineError",
    "PipelineExecutionContext",
    "PipelineMetrics",
    "PipelineOutputs",
    "PipelineResult",
    "run_pipeline",
]
