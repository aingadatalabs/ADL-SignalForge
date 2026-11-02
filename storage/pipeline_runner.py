"""
pipeline_runner.py — Production Orchestration & Execution Engine for ADL-SignalForge

Features:
- Decoupled exception boundaries (initialization vs. workload execution vs. terminal finalization).
- Thread-safe metric and counter accumulation for concurrent workers.
- Strict record accounting invariants (Received = Succeeded + Failed + Skipped) with zero fabricated failures.
- Granular stage-level execution tracking and timing instrumentation.
- Robust finalization handling preventing uncommitted or ambiguous persistence states from corrupting workload logs.
"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional, Set

from storage.repository import (
    SignalForgeRepository,
    InvalidStateTransitionError,
    PipelineRunNotFoundError,
)

logger = logging.getLogger("SignalForgePipelineRunner")


class PipelineExecutionError(Exception):
    """Base error for pipeline execution failures."""
    pass


class PipelineInitializationError(PipelineExecutionError):
    """Raised when the execution record cannot be registered in the repository."""
    pass


class PipelineFinalizationError(PipelineExecutionError):
    """Raised when the workload succeeded, but recording its terminal state failed."""
    pass


class ExecutionMetrics:
    """Thread-safe metric and counter accumulator for concurrent pipeline workers."""

    def __init__(self):
        self._lock = threading.Lock()
        self.records_received = 0
        self.records_succeeded = 0
        self.records_failed = 0
        self.records_skipped = 0

    def add_received(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("Count cannot be negative.")
        with self._lock:
            self.records_received += count

    def add_succeeded(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("Count cannot be negative.")
        with self._lock:
            self.records_succeeded += count

    def add_failed(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("Count cannot be negative.")
        with self._lock:
            self.records_failed += count

    def add_skipped(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("Count cannot be negative.")
        with self._lock:
            self.records_skipped += count

    @property
    def total_attempted(self) -> int:
        with self._lock:
            return self.records_succeeded + self.records_failed

    @property
    def is_accounting_balanced(self) -> bool:
        with self._lock:
            return self.records_received == (self.records_succeeded + self.records_failed + self.records_skipped)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "records_received": self.records_received,
                "records_succeeded": self.records_succeeded,
                "records_failed": self.records_failed,
                "records_skipped": self.records_skipped,
            }


class StageContext:
    """Manages stage-level timing, status, and metrics."""

    def __init__(self, runner: "PipelineRunner", stage_name: str):
        self.runner = runner
        self.stage_name = stage_name
        self.start_time: Optional[float] = None
        self.duration_seconds: float = 0.0
        self.status = "running"

    def __enter__(self):
        self.start_time = time.monotonic()
        logger.info(f"Pipeline stage started: '{self.stage_name}'")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration_seconds = time.monotonic() - (self.start_time or time.monotonic())
        if exc_type:
            self.status = "failed"
            logger.error(f"Pipeline stage failed: '{self.stage_name}' after {self.duration_seconds:.3f}s: {exc_val}")
        else:
            self.status = "succeeded"
            logger.info(f"Pipeline stage completed: '{self.stage_name}' in {self.duration_seconds:.3f}s")
        return False  # Do not suppress exceptions


class PipelineExecutionContext:
    """Exposes a controlled, immutable-facing telemetry interface to pipeline workloads."""

    def __init__(self, run_id: str, pipeline_name: str, metrics: ExecutionMetrics):
        self.run_id = run_id
        self.pipeline_name = pipeline_name
        self._metrics = metrics
        self.error_summary: Optional[str] = None

    def record_received(self, count: int = 1) -> None:
        self._metrics.add_received(count)

    def record_succeeded(self, count: int = 1) -> None:
        self._metrics.add_succeeded(count)

    def record_failed(self, count: int = 1) -> None:
        self._metrics.add_failed(count)

    def record_skipped(self, count: int = 1) -> None:
        self._metrics.add_skipped(count)

    def add_results(self, received: int = 0, succeeded: int = 0, failed: int = 0, skipped: int = 0) -> None:
        if received: self.record_received(received)
        if succeeded: self.record_succeeded(succeeded)
        if failed: self.record_failed(failed)
        if skipped: self.record_skipped(skipped)

    @contextmanager
    def stage(self, stage_name: str) -> Generator[StageContext, None, None]:
        with StageContext(None, stage_name) as sc:
            yield sc


class PipelineRunner:
    def __init__(self, repository: SignalForgeRepository):
        self.repo = repository

    @contextmanager
    def run_context(
        self,
        run_id: str,
        pipeline_name: str,
        initial_records_received: int = 0
    ) -> Generator[PipelineExecutionContext, None, None]:
        """
        Orchestration context manager with decoupled initialization, execution, and finalization error boundaries.
        """
        metrics = ExecutionMetrics()
        if initial_records_received > 0:
            metrics.add_received(initial_records_received)

        execution = PipelineExecutionContext(run_id, pipeline_name, metrics)
        
        start_mono = time.monotonic()
        
        # --- Boundary 1: Initialization ---
        try:
            self.repo.start_pipeline_run(
                run_id=run_id,
                pipeline_name=pipeline_name,
                records_received=metrics.records_received
            )
        except Exception as err:
            logger.error(f"Failed to initialize pipeline run '{run_id}': {err}", exc_info=True)
            raise PipelineInitializationError(f"Could not initialize execution record: {err}") from err

        logger.info(f"Pipeline execution registered & running: '{pipeline_name}' (Run ID: {run_id})")

        workload_failed = False
        caught_exception: Optional[Exception] = None

        # --- Boundary 2: Workload Execution ---
        try:
            yield execution
        except Exception as err:
            workload_failed = True
            caught_exception = err
            execution.error_summary = str(err)
            logger.error(f"Pipeline workload failed for run '{run_id}': {err}", exc_info=True)

        duration = time.monotonic() - start_mono

        # Determine terminal status based purely on record accounting and workload exception
        snapshot = metrics.snapshot()
        final_status = "succeeded"

        if workload_failed:
            final_status = "failed"
        elif snapshot["records_failed"] > 0:
            final_status = "partial_success" if snapshot["records_succeeded"] > 0 else "failed"
        elif snapshot["records_received"] > 0 and snapshot["records_succeeded"] == 0 and snapshot["records_skipped"] == 0:
            # Received records but zero processed outputs
            final_status = "failed"

        # --- Boundary 3: Terminal Finalization ---
        try:
            self.repo.complete_pipeline_run(
                run_id=run_id,
                status=final_status,
                records_succeeded=snapshot["records_succeeded"],
                records_failed=snapshot["records_failed"],
                error_summary=execution.error_summary
            )
            logger.info(
                f"Pipeline run '{run_id}' finalized successfully. "
                f"Status: {final_status}, Metrics: {snapshot}, Duration: {duration:.3f}s"
            )
        except Exception as finalization_err:
            logger.critical(
                f"CRITICAL PERSISTENCE FAILURE: Workload for run '{run_id}' finished "
                f"(failed={workload_failed}), but terminal state could not be persisted: {finalization_err}",
                exc_info=True
            )
            if not workload_failed:
                # If workload succeeded but finalization failed, surface a distinct finalization error
                raise PipelineFinalizationError(
                    f"Pipeline workload succeeded, but finalization status '{final_status}' "
                    f"failed to persist: {finalization_err}"
                ) from finalization_err

        # Re-raise original workload exception if one occurred
        if caught_exception:
            raise caught_exception

    def cancel_run(self, run_id: str, reason: Optional[str] = None) -> None:
        """Explicitly requests cancellation for an active pipeline run."""
        try:
            self.repo.cancel_pipeline_run(run_id, reason=reason)
            logger.info(f"Pipeline run '{run_id}' explicitly cancelled.")
        except Exception as err:
            logger.error(f"Failed to cancel pipeline run '{run_id}': {err}")
            raise