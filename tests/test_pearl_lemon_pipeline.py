import asyncio
import json

from agencies.pearl_lemon import run_pipeline as pipeline
from agencies.pearl_lemon.extraction.extraction import ExtractionRunError


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_pipeline_runs_all_stages_and_propagates_lineage(tmp_path, monkeypatch):
    raw_record = {
        "agency_name": "Example Software",
        "domain": "example.com",
        "website": "https://example.com",
        "description": "B2B workflow software",
        "source_urls": ["https://example.com"],
    }

    async def fake_extraction(*, output_path, pipeline_context, **_kwargs):
        record = {**raw_record, "_pipeline_context": pipeline_context}
        _write_jsonl(output_path, [record])
        return [record]

    async def fake_enrichment(*, output_path, records, **_kwargs):
        result = []
        for resolved in records:
            result.append(
                {
                    **resolved,
                    "company_name": resolved["agency_name"],
                    "company_identity_resolved": True,
                    "website_evidence_available": True,
                    "industry": "B2B SaaS",
                    "country_code": "GB",
                    "business_model": "B2B",
                    "employee_count": 50,
                    "commercial_capacity": True,
                    "business_activity_supported": True,
                    "decision_makers": [
                        {
                            "full_name": "Alex Smith",
                            "title": "CEO",
                            "source_url": "https://example.com/team",
                        }
                    ],
                    "contact_channel_verified": True,
                    "acquisition_needs": ["cold email"],
                    "intent_signals": ["sales hiring"],
                    "enrichment_status": "complete",
                    "enrichment_errors": [],
                }
            )
        _write_jsonl(output_path, result)
        return result

    monkeypatch.setattr(pipeline, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline, "run_enrichment", fake_enrichment)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "extraction" / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized" / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved" / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched" / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified_leads" / "qualified.jsonl",
    )

    result = asyncio.run(pipeline.run_pipeline(config))
    final_record = json.loads(
        config.qualified_output_path.read_text(encoding="utf-8").splitlines()[0]
    )

    assert result.status == "completed"
    assert result.context.agency == "pearl_lemon"
    assert result.metrics.discovered == 1
    assert result.metrics.normalized == 1
    assert result.metrics.identity_resolved == 1
    assert result.metrics.deduplicated == 1
    assert result.metrics.enriched == 1
    assert result.metrics.qualified == 1
    assert final_record["pipeline_context"]["run_id"] == result.context.run_id


def test_pipeline_reports_extraction_partial_results_and_continues(
    tmp_path, monkeypatch
):
    partial_record = {
        "agency_name": "Example Software",
        "domain": "example.com",
        "website": "https://example.com",
        "description": "B2B workflow software",
    }

    async def partial_extraction(*, output_path, pipeline_context, **_kwargs):
        contextual_record = {
            **partial_record,
            "_pipeline_context": pipeline_context,
        }
        _write_jsonl(output_path, [contextual_record])
        raise ExtractionRunError(
            [{"query": "example", "error": "one query failed"}],
            [contextual_record],
        )

    async def empty_enrichment(*, output_path, records, **_kwargs):
        result = []
        _write_jsonl(output_path, result)
        return result

    monkeypatch.setattr(pipeline, "run_extraction", partial_extraction)
    monkeypatch.setattr(pipeline, "run_enrichment", empty_enrichment)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified.jsonl",
    )

    result = asyncio.run(pipeline.run_pipeline(config))

    assert result.status == "partial"
    assert result.metrics.discovered == 1
    assert result.errors[0].stage == "extraction"
    assert result.metrics.normalized == 1
    assert config.qualified_output_path.exists()
