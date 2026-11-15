import json

import pytest

from agencies.pearl_lemon.dedup import (
    DEFAULT_RESOLVED_RECORDS_PATH,
    PearlLemonDeduplicator,
)


def test_default_resolved_path_is_inside_identity_resolution():
    assert DEFAULT_RESOLVED_RECORDS_PATH.parent.name == "identity_resolution"
    assert DEFAULT_RESOLVED_RECORDS_PATH.name == "resolved_records.jsonl"


def test_deduplicator_reads_normalized_jsonl_resolves_and_persists(tmp_path):
    input_path = tmp_path / "normalized_records.jsonl"
    output_path = tmp_path / "identity_resolution" / "resolved_records.jsonl"
    normalized_records = [
        {
            "agency_name": "Acme Software",
            "domain": "example.com",
            "source_urls": ["https://example.com"],
            "extraction_evidence": [{"source_url": "https://example.com"}],
        },
        {
            "agency_name": "ACME SOFTWARE",
            "domain": "example.com",
            "source_urls": ["https://example.com/about"],
            "extraction_evidence": [{"source_url": "https://example.com/about"}],
        },
        {
            "agency_name": "Acme Software",
            "domain": None,
            "source_urls": ["https://search.example/acme"],
            "extraction_evidence": [{"source_url": "https://search.example/acme"}],
        },
        {"agency_name": "Other Company", "domain": "other.example"},
    ]
    input_path.write_text(
        "".join(json.dumps(record) + "\n" for record in normalized_records),
        encoding="utf-8",
    )

    deduplicator = PearlLemonDeduplicator(
        input_path=input_path,
        output_path=output_path,
    )
    resolved_records = deduplicator.process()

    assert len(resolved_records) == 2
    acme = next(record for record in resolved_records if record["domain"] == "example.com")
    assert acme["observation_count"] == 3
    assert len(acme["source_urls"]) == 3
    persisted_records = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted_records == resolved_records


def test_deduplicator_reports_malformed_jsonl_with_line_number(tmp_path):
    input_path = tmp_path / "normalized_records.jsonl"
    input_path.write_text('{"agency_name": "Example"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        PearlLemonDeduplicator(input_path=input_path)


def test_failed_serialization_preserves_previous_snapshot_and_cleans_temp(tmp_path):
    output_path = tmp_path / "identity_resolution" / "resolved_records.jsonl"
    output_path.parent.mkdir()
    output_path.write_text('{"previous": true}\n', encoding="utf-8")
    deduplicator = PearlLemonDeduplicator(
        normalized_records=[
            {
                "agency_name": "Example",
                "domain": "example.com",
                "confidences": {"invalid_json_value": {1, 2}},
            }
        ],
        output_path=output_path,
    )

    with pytest.raises(TypeError):
        deduplicator.process()

    assert output_path.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert list(output_path.parent.glob(".resolved_records.jsonl.*.tmp")) == []
