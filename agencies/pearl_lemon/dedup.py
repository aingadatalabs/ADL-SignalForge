"""Pearl Lemon identity resolution stage."""

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from agencies.adl.identity.entity_resolution import EntityResolver
from agencies.pearl_lemon.normalizer import DEFAULT_NORMALIZED_RECORDS_PATH

DEFAULT_RESOLVED_RECORDS_PATH = (
    Path(__file__).parent / "identity_resolution" / "resolved_records.jsonl"
)


class PearlLemonDeduplicator(EntityResolver):
    """Deduplicate and resolve normalized candidates into a JSONL entity snapshot."""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        *,
        normalized_records: Iterable[dict[str, Any]] | None = None,
        input_path: str | Path = DEFAULT_NORMALIZED_RECORDS_PATH,
        output_path: str | Path = DEFAULT_RESOLVED_RECORDS_PATH,
    ) -> None:
        super().__init__(similarity_threshold=similarity_threshold)
        self.input_path = Path(input_path)
        self.output_path = Path(output_path)
        self.normalized_records = (
            list(normalized_records)
            if normalized_records is not None
            else self._read_records(self.input_path)
        )
        self.resolved_records: list[dict[str, Any]] = []

    @staticmethod
    def _read_records(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as input_file:
            for line_number, line in enumerate(input_file, start=1):
                if not line.strip():
                    continue
                try:
                    record_value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in normalized records file {path} "
                        f"at line {line_number}"
                    ) from exc
                if not isinstance(record_value, dict):
                    raise ValueError(
                        f"Expected a JSON object in {path} at line {line_number}"
                    )
                records.append(cast(dict[str, Any], record_value))
        return records

    def process(self) -> list[dict[str, Any]]:
        """Resolve duplicates and overwrite the resolved-record JSONL snapshot."""
        self.resolved_records = self.resolve(self.normalized_records)
        self._write_resolved_records()
        return self.resolved_records

    def _write_resolved_records(self) -> None:
        """Atomically replace the output while serializing one record at a time."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.output_path.parent,
                prefix=f".{self.output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                for record in self.resolved_records:
                    output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                output_file.flush()
                os.fsync(output_file.fileno())
            temporary_path.replace(self.output_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_RESOLVED_RECORDS_PATH",
    "PearlLemonDeduplicator",
]
