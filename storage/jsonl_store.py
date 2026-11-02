"""
storage/jsonl_store.py — Production-Grade Immutable JSONL Evidence Store

Features:
- Immutable batch segment writes with atomic file renaming and directory fsync.
- Strict producer-shutdown synchronization barrier preventing post-shutdown queue injection.
- Zero silent data drop: failed writes retain payloads in a quarantined buffer rather than discarding via task_done().
- UTC-normalized event-time partitioning (base_dir/YYYY/MM/DD/segments/batch_<uuid>.jsonl).
- Thread-safe operational metrics and explicit lifecycle states (RUNNING, STOPPING, STOPPED, FAILED).
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger("JSONLStore")


class StoreState(Enum):
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RawEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = Field(..., description="Semantic event type identifier")
    event_version: int = Field(default=1)
    run_id: str = Field(..., description="Associated pipeline execution run ID")
    source_provider: str = Field(..., description="Originating data source or agent")
    occurred_at: Optional[datetime] = Field(default=None, description="Source observation timestamp")
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Internal UTC receipt timestamp")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Original unparsed or structured payload")


class JSONLStore:
    def __init__(
        self,
        base_dir: str = "./logs",
        batch_size: int = 100,
        flush_interval_sec: float = 1.0,
        max_queue_size: int = 10000,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be > 0")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")

        self.base_dir = Path(base_dir).resolve()
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec

        self._state = StoreState.RUNNING
        self._state_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)

        # Operational metrics
        self._total_persisted_count = 0
        self._failed_writes_count = 0
        self._quarantined_batches: List[List[Dict[str, Any]]] = []

        # Start non-daemon background worker thread
        self._worker_thread = threading.Thread(
            target=self._background_flusher,
            name="JSONLStoreWorker",
            daemon=False
        )
        self._worker_thread.start()
        logger.info(f"JSONLStore initialized at base path: {self.base_dir}")

    @property
    def state(self) -> StoreState:
        with self._state_lock:
            return self._state

    def log(self, event: RawEvent) -> bool:
        """
        Accepts a validated RawEvent model, serializes it at the producer boundary,
        and enqueues it under a synchronized admission barrier.
        """
        with self._admission_lock:
            with self._state_lock:
                if self._state != StoreState.RUNNING:
                    logger.error(f"Cannot log event. Store state is {self._state.value}.")
                    return False

            if not isinstance(event, RawEvent):
                raise TypeError(f"Payload must be a RawEvent instance, received {type(event)}")

            # Serialize at producer boundary to guarantee immutability & catch errors early
            try:
                serialized_line = json.dumps(
                    event.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":")
                ) + "\n"
            except Exception as err:
                logger.error(f"Failed to serialize RawEvent to JSON: {err}", exc_info=True)
                raise ValueError(f"Serialization failed: {err}") from err

            # Normalize partition key from internal received_at UTC datetime
            received_dt = event.received_at
            if received_dt.tzinfo is None:
                received_dt = received_dt.replace(tzinfo=timezone.utc)
            else:
                received_dt = received_dt.astimezone(timezone.utc)

            partition_key = received_dt.strftime("%Y/%m/%d")

            item = {
                "partition_key": partition_key,
                "event_id": event.event_id,
                "line": serialized_line
            }

            try:
                self._queue.put(item, block=True, timeout=0.5)
                return True
            except queue.Full:
                logger.critical("JSONLStore queue is FULL! Backpressure limit reached. Event rejected.")
                return False

    def _get_target_segment_dir(self, partition_key: str) -> Path:
        segment_dir = self.base_dir / partition_key / "segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        return segment_dir

    def _background_flusher(self) -> None:
        buffer = []
        last_flush_time = time.monotonic()

        while True:
            with self._state_lock:
                current_state = self._state

            is_stopping = current_state in (StoreState.STOPPING, StoreState.FAILED)
            if is_stopping and self._queue.empty():
                break

            try:
                now = time.monotonic()
                timeout = max(0.01, self.flush_interval_sec - (now - last_flush_time))

                try:
                    item = self._queue.get(timeout=timeout)
                    buffer.append(item)
                except queue.Empty:
                    pass

                should_flush = (
                    len(buffer) >= self.batch_size or
                    (buffer and (time.monotonic() - last_flush_time) >= self.flush_interval_sec) or
                    (is_stopping and not self._queue.empty())
                )

                if should_flush and buffer:
                    success = self._flush_buffer(buffer)
                    buffer.clear()
                    last_flush_time = time.monotonic()
                    if not success and current_state == StoreState.RUNNING:
                        with self._state_lock:
                            self._state = StoreState.FAILED

            except Exception as err:
                logger.error(f"Unexpected error in background flusher loop: {err}", exc_info=True)
                with self._state_lock:
                    self._state = StoreState.FAILED

        # Final drain flush on stop
        if buffer:
            try:
                self._flush_buffer(buffer)
            except Exception as err:
                logger.critical(f"Critical error during final buffer flush: {err}", exc_info=True)
                with self._state_lock:
                    self._state = StoreState.FAILED

    def _flush_buffer(self, buffer: list) -> bool:
        """
        Groups buffered items by date partition key and writes them as atomic batch segments
        via temporary files, fsync, and atomic rename. Acknowledges tasks only upon success.
        """
        partitions: Dict[str, list] = {}
        for item in buffer:
            pkey = item["partition_key"]
            if pkey not in partitions:
                partitions[pkey] = []
            partitions[pkey].append(item)

        all_success = True

        for partition_key, items in partitions.items():
            segment_dir = self._get_target_segment_dir(partition_key)
            batch_id = str(uuid.uuid4())
            temp_path = segment_dir / f"batch_{batch_id}.tmp"
            final_path = segment_dir / f"batch_{batch_id}.jsonl"
            payload_str = "".join([i["line"] for i in items])

            success = False
            retries = 3
            backoff = 0.1

            for attempt in range(retries):
                try:
                    with open(temp_path, mode="w", encoding="utf-8") as f:
                        f.write(payload_str)
                        f.flush()
                        os.fsync(f.fileno())

                    # Atomic rename into final segment name
                    os.replace(temp_path, final_path)

                    # Fsync containing directory for durability guarantee across POSIX/Windows
                    try:
                        dir_fd = os.open(str(segment_dir), os.O_RDONLY)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except Exception:
                        pass # Directory fsync optional on certain platforms

                    success = True
                    break
                except IOError as io_err:
                    logger.warning(
                        f"I/O error writing batch segment {final_path} (Attempt {attempt+1}/{retries}): {io_err}"
                    )
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
                    time.sleep(backoff)
                    backoff *= 2

            if success:
                self._total_persisted_count += len(items)
                for _ in items:
                    self._queue.task_done()
            else:
                all_success = False
                self._failed_writes_count += len(items)
                self._quarantined_batches.append(items)
                logger.critical(
                    f"FATAL: Permanently failed to write atomic batch of {len(items)} items to {final_path}! "
                    f"Batch quarantined in memory."
                )
                # DO NOT call task_done() on unpersisted items; keep them accounted for quarantine tracking.

        return all_success

    def close(self, timeout: float = 5.0) -> bool:
        """
        Gracefully stops admission, drains remaining queue items, flushes to disk,
        and joins the worker thread. Returns True only if shutdown completed cleanly.
        """
        with self._admission_lock:
            with self._state_lock:
                if self._state in (StoreState.STOPPED, StoreState.STOPPING):
                    return self._state == StoreState.STOPPED
                if self._state == StoreState.FAILED:
                    logger.error("Cannot cleanly shutdown JSONLStore: store is in FAILED state.")
                    return False
                logger.info("Initiating JSONLStore graceful shutdown...")
                self._state = StoreState.STOPPING

        # Wait for worker to finish processing remaining queue items
        self._worker_thread.join(timeout=timeout)

        with self._state_lock:
            if self._worker_thread.is_alive():
                logger.error("JSONLStore shutdown timed out waiting for worker thread.")
                self._state = StoreState.FAILED
                return False
            
            if self._state != StoreState.FAILED:
                self._state = StoreState.STOPPED
                logger.info(
                    f"JSONLStore shutdown complete. Total persisted: {self._total_persisted_count}, "
                    f"Failed/Quarantined: {self._failed_writes_count}"
                )
                return True
            else:
                logger.error("JSONLStore shutdown completed with persistent write failures.")
                return False