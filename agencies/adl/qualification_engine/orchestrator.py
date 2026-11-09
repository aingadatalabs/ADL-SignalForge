import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field

from core.base import BaseQualifier

from .schemas import (
    BatchItemResult,
    CompanyQualificationInput,
    QualificationBatchResult,
    QualificationPipelineResult,
    RuleEvaluationResult,
    TierAssignmentResult,
)

logger = logging.getLogger("signalforge.qualification.orchestrator")


class QualificationOrchestratorConfig(BaseModel):
    max_concurrency: int = Field(default=20, ge=1, description="Max concurrent candidate evaluations")
    orchestrator_version: str = Field(default="1.0.0", description="Versioning for audit trails")


class QualificationOrchestrator(BaseQualifier[Any, QualificationPipelineResult]):
    def __init__(
        self,
        rule_engine: Any,
        scorer: Any,
        tierer: Any,
        config: QualificationOrchestratorConfig | None = None,
    ):
        self.rule_engine = rule_engine
        self.scorer = scorer
        self.tierer = tierer
        self.config = config or QualificationOrchestratorConfig()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)

    async def evaluate_candidate(
        self,
        normalized_candidate: Any,
        enrichment_profile: Any | None = None,
        verification_report: Any | None = None,
    ) -> QualificationPipelineResult:
        """
        Coordinates sequential evaluation of a single candidate with stage timing and strict identity validation.
        """
        async with self._semaphore:
            evaluation_id = str(uuid.uuid4())
            start_global = time.perf_counter()
            evaluated_at = datetime.now(timezone.utc).isoformat()
            stage_durations: dict[str, float] = {}

            # 1. Strict Identity Validation
            company_id = getattr(normalized_candidate, "company_id", None)
            canonical_domain = getattr(normalized_candidate, "canonical_domain", None)

            if not company_id or company_id == "unknown":
                raise ValueError("Candidate evaluation rejected: missing or invalid 'company_id'.")
            if not canonical_domain:
                raise ValueError(f"Candidate evaluation rejected for company_id={company_id}: missing 'canonical_domain'.")

            logger.info(
                "Starting qualification pipeline for candidate",
                extra={"company_id": company_id, "domain": canonical_domain, "evaluation_id": evaluation_id},
            )

            # 2. Evidence Assembly
            rule_input = self._build_rule_input(normalized_candidate, enrichment_profile)

            # 3. Stage 1: Deterministic Eligibility Rules
            t_start = time.perf_counter()
            try:
                rule_result: RuleEvaluationResult = self.rule_engine.evaluate(rule_input, verification=verification_report)
            except Exception:
                logger.exception("Critical error during rule evaluation", extra={"company_id": company_id})
                raise
            stage_durations["rules_seconds"] = round(time.perf_counter() - t_start, 4)

            score_breakdown = None

            # 4. Stage 2: Commercial Scoring (Only if eligible)
            if rule_result.status == "eligible":
                if not enrichment_profile:
                    # Transition to manual review if profile is missing despite eligibility
                    logger.warning(
                        "Candidate marked eligible but lacks enrichment profile; routing to manual review.",
                        extra={"company_id": company_id}
                    )
                    # Force manual review status to prevent fabricated scoring
                    rule_result = RuleEvaluationResult(
                        company_id=company_id,
                        canonical_domain=canonical_domain,
                        status="manual_review",
                        checks=rule_result.checks,
                        disqualification_reasons=["missing_enrichment_profile"],
                    )
                else:
                    t_start = time.perf_counter()
                    try:
                        score_breakdown = self.scorer.score(enrichment_profile, verification=verification_report)
                    except Exception:
                        logger.exception("Critical error during score calculation", extra={"company_id": company_id})
                        raise
                    stage_durations["scoring_seconds"] = round(time.perf_counter() - t_start, 4)
            else:
                logger.info(
                    "Bypassing scoring engine due to non-eligible rule status",
                    extra={"company_id": company_id, "rule_status": rule_result.status}
                )

            # 5. Stage 3: GTM Tiering & Routing
            t_start = time.perf_counter()
            try:
                tier_assignment: TierAssignmentResult = self.tierer.tier(
                    company_id=company_id,
                    canonical_domain=canonical_domain,
                    rule_result=rule_result,
                    score_breakdown=score_breakdown,
                )
            except Exception:
                logger.exception("Critical error during tier assignment", extra={"company_id": company_id})
                raise
            stage_durations["tiering_seconds"] = round(time.perf_counter() - t_start, 4)

            total_duration = round(time.perf_counter() - start_global, 4)
            stage_durations["total_seconds"] = total_duration

            logger.info(
                "Completed qualification pipeline",
                extra={
                    "company_id": company_id,
                    "outcome": tier_assignment.qualification_outcome,
                    "tier": tier_assignment.tier,
                    "duration_seconds": total_duration,
                },
            )

            return QualificationPipelineResult(
                evaluation_id=evaluation_id,
                company_id=company_id,
                canonical_domain=canonical_domain,
                orchestrator_version=self.config.orchestrator_version,
                evaluated_at=evaluated_at,
                rule_result=rule_result,
                score_breakdown=score_breakdown,
                tier_assignment=tier_assignment,
                total_duration_seconds=total_duration,
                stage_durations=stage_durations,
            )

    async def evaluate_many(
        self, candidates: list[tuple[Any, Any | None, Any | None]]
    ) -> QualificationBatchResult:
        """
        Batch evaluates multiple candidates with bounded concurrency and structured batch outcomes,
        ensuring individual candidate failures do not cause silent data loss.
        """
        batch_id = str(uuid.uuid4())
        tasks = [
            self.evaluate_candidate(cand, prof, ver)
            for cand, prof, ver in candidates
        ]

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        item_results: list[BatchItemResult] = []
        succeeded = 0
        failed = 0

        for idx, res in enumerate(raw_results):
            cand_tuple = candidates[idx]
            cand_obj = cand_tuple[0]
            company_id = getattr(cand_obj, "company_id", f"unknown-index-{idx}")

            if isinstance(res, Exception):
                failed += 1
                error_type = type(res).__name__
                error_message = str(res)
                logger.error(
                    "Candidate evaluation task failed inside batch run",
                    extra={"company_id": company_id, "batch_id": batch_id, "error": error_message},
                    exc_info=res,
                )
                item_results.append(
                    BatchItemResult(
                        company_id=company_id,
                        success=False,
                        error_type=error_type,
                        error_message=error_message,
                    )
                )
            else:
                succeeded += 1
                item_results.append(
                    BatchItemResult(
                        company_id=company_id,
                        success=True,
                        result=res,
                    )
                )

        return QualificationBatchResult(
            batch_id=batch_id,
            total_submitted=len(candidates),
            succeeded=succeeded,
            failed=failed,
            results=item_results,
        )

    def _build_rule_input(self, candidate: Any, profile: Any | None) -> CompanyQualificationInput:
        company_id = getattr(candidate, "company_id", "unknown")
        company_name = getattr(candidate, "company_name", None)
        canonical_domain = getattr(candidate, "canonical_domain", None)
        website = getattr(candidate, "website", None)
        is_unresolved = company_id.startswith("unresolved-") or not canonical_domain

        extracted_meta = getattr(profile, "extraction_metadata", {}) or {} if profile else {}
        description = extracted_meta.get("description") or extracted_meta.get("summary")
        services_raw = extracted_meta.get("services", []) or []
        services = tuple(str(s) for s in services_raw)
        country_code = extracted_meta.get("country") or extracted_meta.get("country_code")
        team_size_text = getattr(profile, "estimated_team_size", None) if profile else None

        return CompanyQualificationInput(
            company_id=company_id,
            company_name=company_name,
            canonical_domain=canonical_domain,
            website=website,
            description=description,
            services=services,
            country_code=country_code,
            team_size_text=team_size_text,
            is_unresolved_candidate=is_unresolved,
        )