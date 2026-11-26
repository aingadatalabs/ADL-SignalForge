"""Pearl Lemon normalization entry point using shared agency rules."""

import copy
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from math import isfinite
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from agencies.adl.extraction.normalizers import AgencyNormalizer
from agencies.pearl_lemon.extraction.extraction import DEFAULT_RAW_RECORDS_PATH

DEFAULT_NORMALIZED_RECORDS_PATH = (
    Path(__file__).parent / "normalized_records" / "normalized_records.jsonl"
)

_NUMBER_TOKEN = r"\d+(?:\s*,\s*\d{3})*(?:\.\d+)?"
_APPROX_PREFIX_RE = re.compile(
    r"^\s*(?:about|approx(?:imately)?\.?|circa|ca\.?|around|roughly|~|≈)\s+",
    flags=re.IGNORECASE,
)
_LOWER_BOUND_SUFFIX_RE = re.compile(
    r"(?:\s*\+|\s+(?:or\s+more|and\s+up|plus))\s*(?:employees?|team\s+members?|staff|people|headcount)?\s*$",
    flags=re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"^\s*(?:(?:about|approx(?:imately)?\.?|circa|ca\.?|around|roughly|~|≈)\s*)?"
    rf"({_NUMBER_TOKEN})\s*(?:[-–—]|\bto\b)\s*({_NUMBER_TOKEN})\s*"
    r"(?:employees?|team\s+members?|staff|people|headcount)?\s*$",
    flags=re.IGNORECASE,
)
_EXACT_OR_APPROX_RE = re.compile(
    rf"^\s*(?:(?:about|approx(?:imately)?\.?|circa|ca\.?|around|roughly|~|≈)\s*)?"
    rf"({_NUMBER_TOKEN})(?!\s*(?:[-–—]|\bto\b)\s*\d)(?:\s*\+|\s+(?:or\s+more|and\s+up|plus))?\s*"
    r"(?:employees?|team\s+members?|staff|people|headcount)?\s*$",
    flags=re.IGNORECASE,
)
_PHONE_PUNCTUATION_RE = re.compile(r"[\s().–—-]+")
_VERIFIED_COMPANY_NAME_SOURCES = frozenset(
    {
        "verified_result_company_name",
        "verified_crawled_title_brand",
    }
)
_VALID_WRITE_MODES = frozenset({"replace", "merge", "skip"})
_ICP_STATUS_VALUES = (
    "in_range",
    "potentially_in_range",
    "out_of_range",
    "unknown",
)
_ICP_CONTRACT_FIELDS = (
    "employee_count",
    "team_size_text",
    "employee_range",
    "team_size",
    "decision_makers",
    "website_evidence_available",
)


class PearlLemonNormalizer(AgencyNormalizer):
    """Normalize raw Pearl Lemon JSONL records while keeping raw, normalized, and verified layers distinct."""

    def __init__(
        self,
        raw_agencies: Iterable[dict[str, Any]] | None = None,
        *,
        input_path: str | Path = DEFAULT_RAW_RECORDS_PATH,
        output_path: str | Path = DEFAULT_NORMALIZED_RECORDS_PATH,
        write_output: bool = True,
        write_mode: str = "replace",
    ) -> None:
        normalized_mode = write_mode.strip().casefold()
        if normalized_mode not in _VALID_WRITE_MODES:
            raise ValueError(
                f"Invalid write_mode {write_mode!r}; expected one of {sorted(_VALID_WRITE_MODES)}"
            )
        self.input_path = Path(input_path)
        self.output_path = Path(output_path)
        self.write_output = bool(write_output)
        self.write_mode = normalized_mode
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
    def _parse_whole_number(token: str) -> int | None:
        cleaned = re.sub(r"\s*,\s*", "", token).strip()
        if not cleaned:
            return None
        if re.fullmatch(r"\d+", cleaned):
            return int(cleaned)
        try:
            numeric = float(cleaned)
        except ValueError:
            return None
        if not isfinite(numeric) or not numeric.is_integer() or numeric < 0:
            return None
        return int(numeric)

    @staticmethod
    def _source_label_from_url(url_candidate: Any) -> str | None:
        if not isinstance(url_candidate, str):
            return None
        trimmed = url_candidate.strip()
        if not trimmed:
            return None
        candidate = trimmed if "://" in trimmed else f"//{trimmed.lstrip('/')}"
        try:
            host = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
        except ValueError:
            return None
        if not host:
            return None
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            return "linkedin"
        return host.removeprefix("www.")

    @classmethod
    def _infer_employee_count_source(
        cls,
        record: Mapping[str, Any] | None = None,
        source_override: str | None = None,
    ) -> str | None:
        if isinstance(source_override, str) and source_override.strip():
            return source_override.strip().casefold()
        if not isinstance(record, Mapping):
            return None

        metadata = record.get("extraction_metadata")
        meta_map = metadata if isinstance(metadata, Mapping) else {}

        for source_key in (
            "employee_count_source",
            "team_size_source",
            "headcount_source",
        ):
            direct_source = record.get(source_key)
            if isinstance(direct_source, str) and direct_source.strip():
                return direct_source.strip().casefold()
            meta_source = meta_map.get(source_key)
            if isinstance(meta_source, str) and meta_source.strip():
                return meta_source.strip().casefold()

        evidence_items = record.get("extraction_evidence")
        if isinstance(evidence_items, Iterable) and not isinstance(
            evidence_items, (str, bytes)
        ):
            for item in evidence_items:
                if not isinstance(item, Mapping):
                    continue
                field_name = item.get("field")
                if field_name in {
                    "employee_count",
                    "team_size_text",
                    "employee_range",
                    "team_size",
                }:
                    ev_source = cls._source_label_from_url(item.get("source_url"))
                    if ev_source:
                        return ev_source

        for url_candidate in (
            meta_map.get("source_url"),
            record.get("source_url"),
            record.get("website"),
            record.get("domain"),
        ):
            url_source = cls._source_label_from_url(url_candidate)
            if url_source:
                return url_source

        return None

    @classmethod
    def _normalize_employee_count_observation(
        cls,
        value: Any,
        *,
        record: Mapping[str, Any] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        inferred_source = cls._infer_employee_count_source(record, source)

        if value is None or isinstance(value, bool):
            return {
                "employee_count_raw": None,
                "employee_count_min": None,
                "employee_count_max": None,
                "employee_count_exact": None,
                "employee_count_is_estimate": False,
                "employee_count_source": inferred_source,
            }

        if isinstance(value, int):
            if value < 0:
                return {
                    "employee_count_raw": str(value),
                    "employee_count_min": None,
                    "employee_count_max": None,
                    "employee_count_exact": None,
                    "employee_count_is_estimate": False,
                    "employee_count_source": inferred_source,
                }
            return {
                "employee_count_raw": value,
                "employee_count_min": value,
                "employee_count_max": value,
                "employee_count_exact": value,
                "employee_count_is_estimate": False,
                "employee_count_source": inferred_source,
            }

        if isinstance(value, float):
            if not isfinite(value) or not value.is_integer() or value < 0:
                return {
                    "employee_count_raw": value,
                    "employee_count_min": None,
                    "employee_count_max": None,
                    "employee_count_exact": None,
                    "employee_count_is_estimate": False,
                    "employee_count_source": inferred_source,
                }
            exact_val = int(value)
            return {
                "employee_count_raw": value,
                "employee_count_min": exact_val,
                "employee_count_max": exact_val,
                "employee_count_exact": exact_val,
                "employee_count_is_estimate": False,
                "employee_count_source": inferred_source,
            }

        if isinstance(value, str):
            raw_text = value.strip()
            if not raw_text:
                return {
                    "employee_count_raw": None,
                    "employee_count_min": None,
                    "employee_count_max": None,
                    "employee_count_exact": None,
                    "employee_count_is_estimate": False,
                    "employee_count_source": inferred_source,
                }

            has_approx_prefix = bool(_APPROX_PREFIX_RE.search(raw_text))
            has_lower_bound = bool(_LOWER_BOUND_SUFFIX_RE.search(raw_text))

            range_match = _RANGE_RE.fullmatch(raw_text)
            if range_match:
                low_val = cls._parse_whole_number(range_match.group(1))
                high_val = cls._parse_whole_number(range_match.group(2))
                if low_val is not None and high_val is not None and low_val <= high_val:
                    return {
                        "employee_count_raw": raw_text,
                        "employee_count_min": low_val,
                        "employee_count_max": high_val,
                        "employee_count_exact": None,
                        "employee_count_is_estimate": has_approx_prefix,
                        "employee_count_source": inferred_source,
                    }

            single_match = _EXACT_OR_APPROX_RE.fullmatch(raw_text)
            if single_match:
                parsed_num = cls._parse_whole_number(single_match.group(1))
                if parsed_num is not None:
                    if has_lower_bound:
                        return {
                            "employee_count_raw": raw_text,
                            "employee_count_min": parsed_num,
                            "employee_count_max": None,
                            "employee_count_exact": None,
                            "employee_count_is_estimate": has_approx_prefix,
                            "employee_count_source": inferred_source,
                        }
                    if has_approx_prefix:
                        return {
                            "employee_count_raw": raw_text,
                            "employee_count_min": parsed_num,
                            "employee_count_max": parsed_num,
                            "employee_count_exact": None,
                            "employee_count_is_estimate": True,
                            "employee_count_source": inferred_source,
                        }
                    return {
                        "employee_count_raw": raw_text,
                        "employee_count_min": parsed_num,
                        "employee_count_max": parsed_num,
                        "employee_count_exact": parsed_num,
                        "employee_count_is_estimate": False,
                        "employee_count_source": inferred_source,
                    }

            return {
                "employee_count_raw": raw_text,
                "employee_count_min": None,
                "employee_count_max": None,
                "employee_count_exact": None,
                "employee_count_is_estimate": has_approx_prefix,
                "employee_count_source": inferred_source,
            }

        return {
            "employee_count_raw": value,
            "employee_count_min": None,
            "employee_count_max": None,
            "employee_count_exact": None,
            "employee_count_is_estimate": False,
            "employee_count_source": inferred_source,
        }

    @classmethod
    def _normalize_employee_count(
        cls,
        value: Any,
        *,
        record: Mapping[str, Any] | None = None,
        source: str | None = None,
    ) -> int | None:
        """Return an exact integer headcount only when unambiguous; otherwise None."""
        obs = cls._normalize_employee_count_observation(
            value, record=record, source=source
        )
        exact = obs.get("employee_count_exact")
        return exact if isinstance(exact, int) else None

    @classmethod
    def _resolve_normalized_employee_count_field(
        cls,
        observation: Mapping[str, Any],
        verified_count: int | None,
    ) -> int | dict[str, Any] | None:
        """Preserve verified exact int, retain min/max bounds for ranges/estimates, or return None."""
        if isinstance(verified_count, int):
            return verified_count

        min_val = observation.get("employee_count_min")
        max_val = observation.get("employee_count_max")
        exact_val = observation.get("employee_count_exact")
        raw_val = observation.get("employee_count_raw")
        is_estimate = bool(observation.get("employee_count_is_estimate"))
        source_val = observation.get("employee_count_source")

        has_bounds = isinstance(min_val, int) or isinstance(max_val, int)
        if not has_bounds and raw_val is None:
            return None

        return {
            "min": min_val if isinstance(min_val, int) else None,
            "max": max_val if isinstance(max_val, int) else None,
            "exact": exact_val if isinstance(exact_val, int) else None,
            "is_estimate": is_estimate,
            "verified": False,
            "raw": raw_val,
            "source": source_val,
        }

    @classmethod
    def _format_phone_syntax(cls, phone: Any) -> str | None:
        """Format phone syntax without claiming E.164 validity or country/entity match."""
        if not isinstance(phone, str):
            return None
        trimmed = phone.strip()
        if not trimmed:
            return None
        formatted = _PHONE_PUNCTUATION_RE.sub("", trimmed).strip()
        return formatted or None

    @classmethod
    def _phone_observation(
        cls,
        field: str,
        raw_value: Any,
        formatted_value: str | None,
        raw_record: Mapping[str, Any],
    ) -> dict[str, Any]:
        digits_only = (
            re.sub(r"\D", "", formatted_value)
            if isinstance(formatted_value, str)
            else ""
        )
        has_leading_plus = (
            isinstance(formatted_value, str) and formatted_value.startswith("+")
        )
        verified_flag = raw_record.get(f"{field}_verified") is True
        return {
            "field": field,
            "raw": raw_value,
            "formatted": formatted_value,
            "digits": digits_only or None,
            "has_leading_plus": has_leading_plus,
            "validated": False,
            "country_inferred": False,
            "entity_attributed": verified_flag,
        }

    @classmethod
    def _verify_phone_fact(
        cls,
        field: str,
        raw_record: Mapping[str, Any],
        formatted_value: str | None,
    ) -> str | None:
        """Emit a verified phone value only when explicitly attributed to this entity."""
        if not formatted_value:
            return None
        if raw_record.get(f"{field}_verified") is True:
            return formatted_value
        return None

    @classmethod
    def _crawl_text_supports_exact_count(
        cls,
        raw_record: Mapping[str, Any],
        exact: int,
    ) -> bool:
        """Check whether crawled website evidence for this record explicitly states the count."""
        evidence_items = raw_record.get("extraction_evidence")
        if not isinstance(evidence_items, Iterable) or isinstance(
            evidence_items, (str, bytes)
        ):
            return False
        spellings = {str(exact), f"{exact:,}"}
        pattern = re.compile(
            rf"(?<!\d)(?:{'|'.join(re.escape(s) for s in spellings)})(?!\d)\s*"
            r"(?:employees?|team\s+members?|staff|people)\b",
            flags=re.IGNORECASE,
        )
        for item in evidence_items:
            if not isinstance(item, Mapping):
                continue
            if item.get("source_type") != "website_crawl":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip() and pattern.search(text):
                return True
        return False

    @classmethod
    def _verify_employee_count_fact(
        cls,
        raw_record: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> int | None:
        """Return a verified exact headcount only when explicitly verified on this entity."""
        exact = observation.get("employee_count_exact")
        if not isinstance(exact, int):
            return None
        if observation.get("employee_count_is_estimate") is True:
            return None

        metadata = raw_record.get("extraction_metadata")
        meta_map = metadata if isinstance(metadata, Mapping) else {}
        unverified_fields = meta_map.get("unverified_fields")
        if isinstance(unverified_fields, Iterable) and not isinstance(
            unverified_fields, (str, bytes)
        ):
            blocked = {str(item) for item in unverified_fields}
            if "employee_count" in blocked or "domain" in blocked:
                return None

        if raw_record.get("parent_company_headcount") is True:
            return None

        if raw_record.get("employee_count_verified") is True:
            return exact

        verified_fields = raw_record.get("verified_fields")
        if isinstance(verified_fields, Iterable) and not isinstance(
            verified_fields, (str, bytes)
        ):
            if "employee_count" in {str(item) for item in verified_fields}:
                return exact

        if cls._crawl_text_supports_exact_count(raw_record, exact):
            return exact

        return None

    @classmethod
    def _verify_company_name_fact(
        cls,
        raw_record: Mapping[str, Any],
        display_name: str | None,
    ) -> str | None:
        """Return a verified display name only when backed by an explicit verified source."""
        if not isinstance(display_name, str) or not display_name.strip():
            return None

        metadata = raw_record.get("extraction_metadata")
        meta_map = metadata if isinstance(metadata, Mapping) else {}
        unverified_fields = meta_map.get("unverified_fields")
        if isinstance(unverified_fields, Iterable) and not isinstance(
            unverified_fields, (str, bytes)
        ):
            if "company_name" in {str(item) for item in unverified_fields}:
                return None

        name_source = meta_map.get("company_name_source")
        has_verified_source = (
            isinstance(name_source, str)
            and name_source.strip() in _VERIFIED_COMPANY_NAME_SOURCES
        )
        if raw_record.get("name_confidence") == "high" or has_verified_source:
            return display_name

        return None

    @classmethod
    def _build_normalization_contract(
        cls,
        raw_record: Mapping[str, Any],
        normalized_record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Document record stage and ICP evaluation contract for downstream stages."""
        raw_stage = raw_record.get("_pipeline_stage")
        input_stage = (
            raw_stage.strip()
            if isinstance(raw_stage, str) and raw_stage.strip()
            else "raw_or_partial"
        )
        present_fields = [
            field for field in _ICP_CONTRACT_FIELDS if field in normalized_record
        ]
        return {
            "stage": "normalized",
            "input_stage": input_stage,
            "icp_target": "PearlLemonICP.evaluate",
            "icp_compatible": True,
            "expected_icp_statuses": list(_ICP_STATUS_VALUES),
            "present_icp_fields": present_fields,
        }

    @classmethod
    def normalize_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        """Clean syntax in one candidate while preserving raw, normalized, and verified layers."""
        # Capture pre-normalization snapshot before super() runs so any in-place
        # mutations in AgencyNormalizer.normalize_record do not taint raw evidence.
        raw_snapshot = copy.deepcopy(record)

        raw_observations: dict[str, Any] = {}
        for field in (
            "company_name",
            "phone",
            "phone_number",
            "employee_count",
            "team_size_text",
            "employee_range",
            "team_size",
        ):
            if field in raw_snapshot:
                raw_observations[field] = copy.deepcopy(raw_snapshot[field])

        company_display: str | None = None
        company_normalized: str | None = None
        verified_company_name: str | None = None
        raw_name_val: Any = None
        has_company_name = "company_name" in raw_snapshot
        if has_company_name:
            raw_name_val = raw_snapshot["company_name"]
            if isinstance(raw_name_val, str) and raw_name_val.strip():
                company_display = re.sub(r"\s+", " ", raw_name_val.strip())
                company_normalized = cls.clean_name(company_display).casefold()
                verified_company_name = cls._verify_company_name_fact(
                    raw_snapshot, company_display
                )

        phone_updates: dict[str, dict[str, Any]] = {}
        for field in ("phone", "phone_number"):
            if field in raw_snapshot:
                raw_phone = raw_snapshot[field]
                formatted_phone = cls._format_phone_syntax(raw_phone)
                phone_updates[field] = {
                    "raw": raw_phone,
                    "formatted": formatted_phone,
                    "observation": cls._phone_observation(
                        field, raw_phone, formatted_phone, raw_snapshot
                    ),
                    "verified": cls._verify_phone_fact(
                        field, raw_snapshot, formatted_phone
                    ),
                }

        raw_headcount_source_value: Any = None
        if "employee_count" in raw_snapshot and raw_snapshot["employee_count"] is not None:
            raw_headcount_source_value = raw_snapshot["employee_count"]
        else:
            for fallback_field in ("team_size_text", "employee_range", "team_size"):
                if (
                    fallback_field in raw_snapshot
                    and raw_snapshot[fallback_field] is not None
                ):
                    raw_headcount_source_value = raw_snapshot[fallback_field]
                    break

        has_headcount = (
            "employee_count" in raw_snapshot or raw_headcount_source_value is not None
        )
        headcount_observation: dict[str, Any] | None = None
        verified_count: int | None = None
        resolved_employee_count: int | dict[str, Any] | None = None
        if has_headcount:
            headcount_observation = cls._normalize_employee_count_observation(
                raw_headcount_source_value,
                record=raw_snapshot,
            )
            verified_count = cls._verify_employee_count_fact(
                raw_snapshot, headcount_observation
            )
            resolved_employee_count = cls._resolve_normalized_employee_count_field(
                headcount_observation, verified_count
            )

        normalized = super().normalize_record(record)

        if has_company_name:
            normalized["company_name_raw"] = raw_name_val
            normalized["company_name"] = company_display
            normalized["company_name_normalized"] = company_normalized
            normalized["verified_company_name"] = verified_company_name

        for field, phone_data in phone_updates.items():
            normalized[f"{field}_raw"] = phone_data["raw"]
            normalized[field] = phone_data["formatted"]
            normalized[f"{field}_observation"] = phone_data["observation"]
            normalized[f"verified_{field}"] = phone_data["verified"]

        if has_headcount and headcount_observation is not None:
            normalized["raw_observations"] = raw_observations
            normalized["employee_count_observation"] = headcount_observation
            normalized["verified_employee_count"] = verified_count
            normalized["employee_count"] = resolved_employee_count

        # Preserve ICP evaluation fields and declare stage contract explicitly.
        for icp_field in (
            "team_size_text",
            "employee_range",
            "team_size",
            "decision_makers",
            "website_evidence_available",
        ):
            if icp_field in raw_snapshot and icp_field not in normalized:
                normalized[icp_field] = copy.deepcopy(raw_snapshot[icp_field])

        normalized["_pipeline_stage"] = "normalized"
        normalized["_normalization_contract"] = cls._build_normalization_contract(
            raw_snapshot, normalized
        )

        return normalized

    @staticmethod
    def _record_merge_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
        domain = ""
        for d_key in ("canonical_domain", "domain"):
            d_val = record.get(d_key)
            if isinstance(d_val, str) and d_val.strip():
                domain = d_val.strip().casefold().removeprefix("www.")
                break

        norm_name = record.get("company_name_normalized")
        name_key = (
            norm_name.strip().casefold()
            if isinstance(norm_name, str) and norm_name.strip()
            else ""
        )
        if not name_key:
            raw_name = record.get("company_name")
            name_key = (
                raw_name.strip().casefold()
                if isinstance(raw_name, str) and raw_name.strip()
                else ""
            )

        source_url = ""
        meta = record.get("extraction_metadata")
        meta_map = meta if isinstance(meta, Mapping) else {}
        for u_val in (meta_map.get("source_url"), record.get("source_url")):
            if isinstance(u_val, str) and u_val.strip():
                source_url = u_val.strip()
                break

        return (domain, name_key, source_url)

    @classmethod
    def _merge_with_existing_snapshot(
        cls,
        existing_records: Iterable[dict[str, Any]],
        incoming_records: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        index_by_key: dict[tuple[str, str, str], int] = {}

        for rec in existing_records:
            key = cls._record_merge_key(rec)
            if key != ("", "", "") and key in index_by_key:
                merged[index_by_key[key]] = rec
            else:
                if key != ("", "", ""):
                    index_by_key[key] = len(merged)
                merged.append(rec)

        for rec in incoming_records:
            key = cls._record_merge_key(rec)
            if key != ("", "", "") and key in index_by_key:
                merged[index_by_key[key]] = rec
            else:
                if key != ("", "", ""):
                    index_by_key[key] = len(merged)
                merged.append(rec)

        return merged

    @staticmethod
    def _write_jsonl_atomic(
        records: Iterable[Mapping[str, Any]],
        target_path: Path,
    ) -> None:
        payload = "".join(
            json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            for record in records
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                temp_path = Path(tmp_file.name)
                tmp_file.write(payload)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            temp_path.replace(target_path)
        except Exception:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def process(
        self,
        *,
        write_output: bool | None = None,
        write_mode: str | None = None,
    ) -> dict[str, Any]:
        """Normalize records independently and optionally persist snapshot atomically."""
        should_write = self.write_output if write_output is None else bool(write_output)
        effective_mode = (
            self.write_mode
            if write_mode is None
            else write_mode.strip().casefold()
        )
        if effective_mode not in _VALID_WRITE_MODES:
            raise ValueError(
                f"Invalid write_mode {write_mode!r}; expected one of {sorted(_VALID_WRITE_MODES)}"
            )

        result = super().process()
        self.normalized_records = result["records"]

        if should_write and effective_mode != "skip":
            records_to_write = self.normalized_records
            if effective_mode == "merge" and self.output_path.exists():
                existing = self._read_records(self.output_path)
                records_to_write = self._merge_with_existing_snapshot(
                    existing, self.normalized_records
                )
            self._write_jsonl_atomic(records_to_write, self.output_path)

        return result


__all__ = ["DEFAULT_NORMALIZED_RECORDS_PATH", "PearlLemonNormalizer"]