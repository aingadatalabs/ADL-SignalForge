import os
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union

# Attempt to load high-performance orjson
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False

# Optional file locking support for multi-process safety
try:
    from filelock import FileLock
    HAS_FILELOCK = True
except ImportError:
    HAS_FILELOCK = False

logger = logging.getLogger(__name__)


class JSONLRepository:
    """Production-grade transactional repository for JSONL files.

    Features:
    - Atomic overwrites via temporary files and conditional fsync.
    - Configurable disk durability vs. throughput (enable_fsync toggle).
    - Strict schema validation (enforces dictionary record contracts).
    - Explicit multi-process locking policy.
    - Memory-efficient streaming reads and writes.
    """

    def __init__(
        self, 
        filepath: Union[str, Path], 
        encoding: str = "utf-8", 
        enable_locking: bool = True,
        lock_timeout: float = 10.0,
        enable_fsync: bool = True
    ):
        self.filepath = Path(filepath)
        self.encoding = encoding
        self.lock_timeout = lock_timeout
        self.enable_fsync = enable_fsync
        
        # Ensure parent directory exists
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Concurrency locking setup with strict policy enforcement
        self._lock = None
        if enable_locking:
            if not HAS_FILELOCK:
                raise RuntimeError(
                    "File locking is enabled (enable_locking=True), but the 'filelock' "
                    "package is not installed. Install 'filelock' or explicitly disable locking."
                )
            lock_path = self.filepath.with_suffix(self.filepath.suffix + ".lock")
            self._lock = FileLock(str(lock_path), timeout=self.lock_timeout)

    def _parse_line(self, line: str) -> Any:
        """Parses a single line using available JSON engine."""
        if HAS_ORJSON:
            return orjson.loads(line)
        return json.loads(line)

    def _serialize_record(self, record: Dict[str, Any]) -> bytes:
        """Serializes a record strictly to UTF-8 bytes."""
        if not isinstance(record, dict):
            raise TypeError(f"Record must be a dictionary, got {type(record).__name__}")
        
        if HAS_ORJSON:
            return orjson.dumps(record, option=orjson.OPT_NON_STR_KEYS)
        else:
            return json.dumps(record, ensure_ascii=False, allow_nan=False).encode(self.encoding)

    def read_records(self) -> Generator[Dict[str, Any], None, None]:
        """Reads a JSONL file line by line with robust error isolation and type validation."""
        if not self.filepath.exists():
            logger.warning("Repository file not found: %s", self.filepath)
            return

        with open(self.filepath, "r", encoding=self.encoding) as f:
            for line_num, line in enumerate(f, start=1):
                stripped_line = line.strip()
                if not stripped_line:
                    continue

                try:
                    record = self._parse_line(stripped_line)
                except Exception as e:
                    logger.error(
                        "Malformed JSON line at %s:%d - Error: %s",
                        self.filepath,
                        line_num,
                        str(e),
                    )
                    continue

                if not isinstance(record, dict):
                    logger.warning(
                        "Skipping non-object JSONL record (type: %s) at %s:%d",
                        type(record).__name__,
                        self.filepath,
                        line_num,
                    )
                    continue

                yield record

    def read_batches(self, batch_size: int = 100) -> Generator[List[Dict[str, Any]], None, None]:
        """Yields records in fixed-size batches to optimize pipeline throughput."""
        if batch_size < 1:
            raise ValueError("Batch size must be a positive integer.")

        batch: List[Dict[str, Any]] = []
        for record in self.read_records():
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def write_record(self, record: Dict[str, Any]) -> None:
        """Appends a single record thread/process-safely."""
        self.write_batch([record])

    def write_batch(self, records: Iterable[Dict[str, Any]]) -> int:
        """Appends a collection of records incrementally under file lock.

        Returns:
            The total number of records successfully written.
        """
        count = 0
        context = self._lock if self._lock else _NullContext()

        with context:
            with open(self.filepath, "ab") as f:
                for record in records:
                    serialized = self._serialize_record(record)
                    f.write(serialized + b"\n")
                    count += 1
                f.flush()
                if self.enable_fsync:
                    os.fsync(f.fileno())

        return count

    def overwrite(self, records: Iterable[Dict[str, Any]]) -> int:
        """Overwrites the repository file atomically using a temporary staging file.

        Returns:
            The total number of records written.
        """
        count = 0
        context = self._lock if self._lock else _NullContext()

        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=self.filepath.parent,
            prefix=f".{self.filepath.name}.tmp."
        )
        temp_path = Path(temp_path_str)

        try:
            with context:
                with os.fdopen(temp_fd, "wb") as f:
                    for record in records:
                        serialized = self._serialize_record(record)
                        f.write(serialized + b"\n")
                        count += 1
                    f.flush()
                    if self.enable_fsync:
                        os.fsync(f.fileno())

                os.replace(temp_path, self.filepath)
                
                if self.enable_fsync:
                    try:
                        dir_fd = os.open(str(self.filepath.parent), os.O_RDONLY)
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except OSError:
                        pass

        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        return count


class _NullContext:
    """Fallback dummy context manager when file locking is disabled."""
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass