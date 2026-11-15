import json

import pytest

from agencies.pearl_lemon.normalizer import (
    DEFAULT_NORMALIZED_RECORDS_PATH,
    PearlLemonNormalizer,
)


def test_default_normalized_records_path_is_under_normalized_records():
    assert DEFAULT_NORMALIZED_RECORDS_PATH.parent.name == "normalized_records"
    assert DEFAULT_NORMALIZED_RECORDS_PATH.name == "normalized_records.jsonl"


def test_normalizer_reads_jsonl_and_normalizes_record_fields(tmp_path):
    input_path = tmp_path / "raw_records.jsonl"
    output_path = tmp_path / "normalized_records" / "normalized_records.jsonl"
    raw_record = {
        "agency_name": "  Example   Software ",
        "company_name": "  ACME   INC. ",
        "phone_number": "+1 (555) 123-4567",
        "employee_count": "about 1,200 employees",
        "team_size_text": "1,000-1,500",
        "website": "https://example.com",
    }
    input_path.write_text(json.dumps(raw_record) + "\n", encoding="utf-8")

    normalizer = PearlLemonNormalizer(
        input_path=input_path,
        output_path=output_path,
    )
    result = normalizer.process()

    assert result["records"][0]["agency_name"] == "Example Software"
    assert result["records"][0]["company_name"] == "acme inc."
    assert result["records"][0]["phone_number"] == "+15551234567"
    assert result["records"][0]["employee_count"] == 1200
    assert result["records"][0]["team_size_text"] == "1,000-1,500"
    assert normalizer.normalized_records == result["records"]
    persisted_records = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted_records == normalizer.normalized_records
    normalizer.process()
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == len(
        normalizer.normalized_records
    )


def test_normalizer_does_not_guess_employee_count_ranges():
    normalized = PearlLemonNormalizer.normalize_record(
        {"agency_name": "Example", "employee_count": "10-50"}
    )

    assert normalized["employee_count"] is None


def test_normalizer_reports_malformed_jsonl_with_line_number(tmp_path):
    input_path = tmp_path / "raw_records.jsonl"
    input_path.write_text('{"agency_name": "Example"}\ninvalid\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        PearlLemonNormalizer(input_path=input_path)
