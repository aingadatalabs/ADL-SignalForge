"""Pearl Lemon normalization entry point using shared agency rules."""

import json
import re
from collections.abc import Iterable
from math import isfinite
from pathlib import Path
from typing import Any, cast

from agencies.adl.extraction.normalizers import AgencyNormalizer
from agencies.pearl_lemon.extraction.extraction import DEFAULT_RAW_RECORDS_PATH

DEFAULT_NORMALIZED_RECORDS_PATH = (
    Path(__file__).parent / "normalized_records" / "normalized_records.jsonl"
)


class PearlLemonNormalizer(AgencyNormalizer):
    """Normalize raw Pearl Lemon JSONL records without comparing candidates."""

    def __init__(
        self,
        raw_agencies: Iterable[dict[str, Any]] | None = None,
        *,
        input_path: str | Path = DEFAULT_RAW_RECORDS_PATH,
        output_path: str | Path = DEFAULT_NORMALIZED_RECORDS_PATH,
    ) -> None:
        self.input_path = Path(input_path)
        self.output_path = Path(output_path)
        records = (
            list(raw_agencies)
            if raw_agencies is not None
            else self._read_records(self.input_path)
        )
        super().__init__(records)
        self.normalized_records: list[dict[str, Any]] = []

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
                        f"Invalid JSON in raw records file {path} at line {line_number}"
                    ) from exc
                if not isinstance(record_value, dict):
                    raise ValueError(
                        f"Expected a JSON object in {path} at line {line_number}"
                    )
                record = cast(dict[str, Any], record_value)
                records.append(record)
        return records

    @staticmethod
    def _normalize_employee_count(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if isfinite(value) and value.is_integer() else None
        if isinstance(value, str):
            match = re.fullmatch(
                r"\s*(?:(?:about|approx(?:imately)?\.?)\s*)?"
                r"(\d[\d,]*)\s*(?:\+|employees?|staff|people)?\s*",
                value,
                flags=re.IGNORECASE,
            )
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    @classmethod
    def normalize_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        """Clean syntax in one candidate while preserving its raw observations."""
        normalized = super().normalize_record(record)
        company_name = normalized.get("company_name")
        if isinstance(company_name, str):
            normalized["company_name"] = cls.clean_name(company_name).casefold()

        for field in ("phone", "phone_number"):
            phone = normalized.get(field)
            if isinstance(phone, str):
                normalized[field] = re.sub(r"[\s().-]", "", phone).strip()

        if "employee_count" in normalized:
            normalized["employee_count"] = cls._normalize_employee_count(
                normalized["employee_count"]
            )
        return normalized

    def process(self) -> dict[str, Any]:
        """Normalize records independently and save the complete JSONL snapshot."""
        result = super().process()
        self.normalized_records = result["records"]
        lines = [
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in self.normalized_records
        ]
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("".join(lines), encoding="utf-8")
        return result


__all__ = ["DEFAULT_NORMALIZED_RECORDS_PATH", "PearlLemonNormalizer"]
