import asyncio
import inspect
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

# Assuming these are imported from your schema module:
# from schemas import (
#     NormalizedCandidate,
#     CrawlResult,
#     AgencyEnrichmentProfile,
#     VerificationReport,
# )

logger = logging.getLogger("signalforge.orchestrator")

ExecutionStatus = Literal["success", "partial_success", "failed"]


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    max_concurrent_companies: int = 5
    crawl_timeout: float = 45.0
    extraction_timeout: float = 45.0
    verification_timeout: float = 20.0
    pipeline_timeout: float = 120.0
    pipeline_version: str = "1.0.0"


class EnrichmentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    execution_id: str
    company_id: str
    canonical_domain: str
    status: ExecutionStatus
    crawl_result: Any | None = None          # CrawlResult | None
    profile: Any | None = None               # AgencyEnrichmentProfile | None
    verification: Any | None = None          # VerificationReport | None
    success: bool = True                     # Backward compatibility flag
    error_message: str | None = None
    orchestration_metadata: dict[str, Any] = Field(default_factory=dict)


class EnrichmentOrchestrator:
    def __init__(
        self,
        crawler_fn: Callable[[Any], Any],
        extractor_fn: Callable[[Any], Any],
        verifier_fn: Callable[[Any], Any],
        config: OrchestratorConfig | None = None,
    ):
        self.crawler_fn = crawler_fn
        self.extractor_fn = extractor_fn
        self.verifier_fn = verifier_fn
        self.config = config or OrchestratorConfig()
        self._company_semaphore = asyncio.Semaphore(self.config.max_concurrent_companies)

    async def run(self, candidate: Any) -> EnrichmentResult:
        """Runs the pipeline for a single company within global concurrency bounds."""
        async with self._company_semaphore:
            try:
                async with asyncio.timeout(self.config.pipeline_timeout):
                    return await self._run_pipeline(candidate)
            except asyncio.TimeoutError:
                execution_id = str(uuid.uuid4())
                logger.error(
                    "Pipeline timed out globally",
                    extra={"company_id": candidate.company_id, "execution_id": execution_id},
                )
                return EnrichmentResult(
                    execution_id=execution_id,
                    company_id=candidate.company_id,
                    canonical_domain=candidate.canonical_domain,
                    status="failed",
                    success=False,
                    error_message="[pipeline_timeout] Global pipeline timeout exceeded",
                )

    async def run_many(self, candidates: list[Any]) -> list[EnrichmentResult]:
        """Executes enrichment across a batch of candidates using the concurrency semaphore."""
        return await asyncio.gather(*(self.run(c) for c in candidates))

    async def _run_pipeline(self, candidate: Any) -> EnrichmentResult:
        execution_id = str(uuid.uuid4())
        start_time = datetime.now(timezone.utc)
        start_perf = time.perf_counter()
        
        company_id = candidate.company_id
        domain = candidate.canonical_domain

        logger.info(
            "Starting enrichment pipeline",
            extra={"company_id": company_id, "domain": domain, "execution_id": execution_id},
        )

        metadata: dict[str, Any] = {
            "execution_id": execution_id,
            "pipeline_version": self.config.pipeline_version,
            "started_at": start_time.isoformat(),
            "stages": {},
        }

        crawl_result: Any | None = None
        profile: Any | None = None
        verification: Any | None = None

        try:
            # --- Stage 1: Crawling ---
            stage_start_perf = time.perf_counter()
            try:
                crawl_result = await self._execute_stage(
                    "crawl",
                    lambda: self.crawler_fn(candidate),
                    timeout=self.config.crawl_timeout,
                )
                
                # Validate output type
                if not hasattr(crawl_result, "pages"):
                    raise TypeError("Crawler output must provide 'pages' attribute")

                metadata["stages"]["crawl"] = {
                    "status": "success",
                    "duration_seconds": time.perf_counter() - stage_start_perf,
                    "pages_fetched": len(crawl_result.pages),
                }
            except asyncio.CancelledError:
                raise
            except Exception as e:
                duration = time.perf_counter() - stage_start_perf
                logger.exception("Crawl stage failed", extra={"company_id": company_id, "stage": "crawl"})
                metadata["stages"]["crawl"] = {"status": "failed", "duration_seconds": duration, "error": str(e)}
                return self._build_result(
                    execution_id, company_id, domain, "failed", crawl_result, profile, verification, metadata, start_perf, f"[crawl_failed] {type(e).__name__}"
                )

            # --- Stage 2: Extraction ---
            stage_start_perf = time.perf_counter()
            try:
                profile = await self._execute_stage(
                    "extract",
                    lambda: self.extractor_fn(crawl_result),
                    timeout=self.config.extraction_timeout,
                )

                if not hasattr(profile, "decision_makers"):
                    raise TypeError("Extractor output must provide 'decision_makers' attribute")

                metadata["stages"]["extract"] = {
                    "status": "success",
                    "duration_seconds": time.perf_counter() - stage_start_perf,
                    "decision_makers_found": len(profile.decision_makers),
                    "contacts_found": len(profile.contacts),
                }
            except asyncio.CancelledError:
                raise
            except Exception as e:
                duration = time.perf_counter() - stage_start_perf
                logger.exception("Extraction stage failed", extra={"company_id": company_id, "stage": "extract"})
                metadata["stages"]["extract"] = {"status": "failed", "duration_seconds": duration, "error": str(e)}
                return self._build_result(
                    execution_id, company_id, domain, "partial_success", crawl_result, profile, verification, metadata, start_perf, f"[extraction_failed] {type(e).__name__}"
                )

            # --- Stage 3: Verification ---
            stage_start_perf = time.perf_counter()
            try:
                verification = await self._execute_stage(
                    "verify",
                    lambda: self.verifier_fn(profile),
                    timeout=self.config.verification_timeout,
                )

                if not hasattr(verification, "email_candidates"):
                    raise TypeError("Verifier output must provide 'email_candidates' attribute")

                metadata["stages"]["verify"] = {
                    "status": "success",
                    "duration_seconds": time.perf_counter() - stage_start_perf,
                    "candidates_verified": len(verification.email_candidates),
                }
            except asyncio.CancelledError:
                raise
            except Exception as e:
                duration = time.perf_counter() - stage_start_perf
                logger.exception("Verification stage failed", extra={"company_id": company_id, "stage": "verify"})
                metadata["stages"]["verify"] = {"status": "failed", "duration_seconds": duration, "error": str(e)}
                return self._build_result(
                    execution_id, company_id, domain, "partial_success", crawl_result, profile, verification, metadata, start_perf, f"[verification_failed] {type(e).__name__}"
                )

            # Full Success
            total_duration = time.perf_counter() - start_perf
            metadata["total_duration_seconds"] = total_duration
            metadata["completed_at"] = datetime.now(timezone.utc).isoformat()

            logger.info("Successfully completed enrichment pipeline", extra={"company_id": company_id, "execution_id": execution_id})

            return EnrichmentResult(
                execution_id=execution_id,
                company_id=company_id,
                canonical_domain=domain,
                status="success",
                crawl_result=crawl_result,
                profile=profile,
                verification=verification,
                success=True,
                error_message=None,
                orchestration_metadata=metadata,
            )

        except asyncio.CancelledError:
            logger.info("Enrichment cancelled", extra={"company_id": company_id, "execution_id": execution_id})
            raise

        except Exception as e:
            logger.exception("Unhandled critical error in orchestrator", extra={"company_id": company_id})
            return self._build_result(
                execution_id, company_id, domain, "failed", crawl_result, profile, verification, metadata, start_perf, f"[critical_error] {type(e).__name__}"
            )

    async def _execute_stage(self, stage_name: str, fn: Callable[[], Any], timeout: float) -> Any:
        """Executes a stage with timeout support, properly handling sync/async functions without event loop blocking."""
        async def _wrapper():
            result = fn()
            if inspect.isawaitable(result):
                return await result
            if asyncio.iscoroutinefunction(fn):
                return await fn()
            return await asyncio.to_thread(fn)

        async with asyncio.timeout(timeout):
            return await _wrapper()

    def _build_result(
        self,
        execution_id: str,
        company_id: str,
        domain: str,
        status: ExecutionStatus,
        crawl_result: Any | None,
        profile: Any | None,
        verification: Any | None,
        metadata: dict[str, Any],
        start_perf: float,
        error_msg: str,
    ) -> EnrichmentResult:
        total_duration = time.perf_counter() - start_perf
        metadata["total_duration_seconds"] = total_duration
        metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
        metadata["failed_stage_error"] = error_msg

        return EnrichmentResult(
            execution_id=execution_id,
            company_id=company_id,
            canonical_domain=domain,
            status=status,
            crawl_result=crawl_result,
            profile=profile,
            verification=verification,
            success=(status == "success"),
            error_message=error_msg,
            orchestration_metadata=metadata,
        )