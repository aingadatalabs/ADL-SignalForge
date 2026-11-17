import asyncio
import json
from pathlib import Path

import pytest

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
    assert result.outputs.raw_records != str(config.raw_output_path)
    assert result.outputs.enriched_records != str(config.enriched_output_path)
    assert result.outputs.qualified_leads != str(config.qualified_output_path)
    assert result.outputs.published_qualified_leads == str(
        config.qualified_output_path
    )
    assert result.context.run_id in result.outputs.raw_records
    assert result.context.run_id in result.outputs.qualified_leads


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
    assert result.errors[0].details == {
        "failures": [{"query": "example", "error": "one query failed"}],
        "partial_record_count": 1,
    }
    assert result.metrics.normalized == 1
    assert config.qualified_output_path.exists()


def test_pipeline_config_rejects_overlapping_paths_and_zero_similarity():
    with pytest.raises(ValueError, match="non-overlapping"):
        pipeline.PearlLemonPipelineConfig(
            raw_output_path="records.jsonl",
            normalized_output_path="records.jsonl",
        )

    with pytest.raises(ValueError, match="greater than 0"):
        pipeline.PearlLemonPipelineConfig(similarity_threshold=0)


def test_pipeline_accepts_max_results_per_query_keyword(tmp_path, monkeypatch):
    observed_limits = []

    async def empty_extraction(*, max_results_per_query, output_path, **_kwargs):
        observed_limits.append(max_results_per_query)
        _write_jsonl(output_path, [])
        return []

    async def empty_enrichment(*, output_path, **_kwargs):
        _write_jsonl(output_path, [])
        return []

    monkeypatch.setattr(pipeline, "run_extraction", empty_extraction)
    monkeypatch.setattr(pipeline, "run_enrichment", empty_enrichment)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified.jsonl",
    )

    result = asyncio.run(
        pipeline.run_pipeline(config, max_results_per_query=1)
    )

    assert result.status == "partial"
    assert result.metrics.discovered == 0
    assert result.errors[0].error_type == "NoRecordsDiscovered"
    assert result.outputs.qualified_leads is not None
    assert Path(result.outputs.qualified_leads).read_text(encoding="utf-8") == ""
    assert result.outputs.published_qualified_leads is None
    assert observed_limits == [1]


def test_empty_discovery_does_not_replace_published_qualified_leads(
    tmp_path, monkeypatch
):
    async def empty_extraction(*, output_path, **_kwargs):
        _write_jsonl(output_path, [])
        return []

    async def empty_enrichment(*, output_path, **_kwargs):
        _write_jsonl(output_path, [])
        return []

    monkeypatch.setattr(pipeline, "run_extraction", empty_extraction)
    monkeypatch.setattr(pipeline, "run_enrichment", empty_enrichment)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified.jsonl",
    )
    config.qualified_output_path.write_text('{"existing":true}\n', encoding="utf-8")

    result = asyncio.run(pipeline.run_pipeline(config))

    assert result.status == "partial"
    assert result.outputs.published_qualified_leads is None
    assert config.qualified_output_path.read_text(encoding="utf-8") == (
        '{"existing":true}\n'
    )


def test_failed_run_does_not_report_preexisting_qualified_output(
    tmp_path, monkeypatch
):
    async def failed_extraction(**_kwargs):
        raise RuntimeError("discovery unavailable")

    monkeypatch.setattr(pipeline, "run_extraction", failed_extraction)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified.jsonl",
    )
    config.qualified_output_path.write_text('{"stale":true}\n', encoding="utf-8")

    result = asyncio.run(pipeline.run_pipeline(config))

    assert result.status == "failed"
    assert result.outputs.qualified_leads is None
    assert result.outputs.published_qualified_leads is None
    assert config.qualified_output_path.read_text(encoding="utf-8") == (
        '{"stale":true}\n'
    )


def test_concurrent_runs_keep_stage_artifacts_isolated(tmp_path, monkeypatch):
    async def fake_extraction(
        *, client, output_path, pipeline_context, **_kwargs
    ):
        record = {
            "agency_name": f"{client} Company",
            "domain": f"{client.casefold()}.example",
            "website": f"https://{client.casefold()}.example",
            "_pipeline_context": pipeline_context,
        }
        _write_jsonl(output_path, [record])
        await asyncio.sleep(0)
        return [record]

    async def fake_enrichment(*, output_path, records, **_kwargs):
        enriched = [
            {
                **record,
                "enrichment_status": "complete",
                "enrichment_errors": [],
            }
            for record in records
        ]
        _write_jsonl(output_path, enriched)
        await asyncio.sleep(0)
        return enriched

    async def fake_qualification(*, input_path, output_path, **_kwargs):
        enriched = [
            json.loads(line)
            for line in input_path.read_text(encoding="utf-8").splitlines()
        ]
        _write_jsonl(
            output_path,
            [
                {
                    "agency_name": record["agency_name"],
                    "_pipeline_context": record["_pipeline_context"],
                }
                for record in enriched
            ],
        )
        return []

    monkeypatch.setattr(pipeline, "run_extraction", fake_extraction)
    monkeypatch.setattr(pipeline, "run_enrichment", fake_enrichment)
    monkeypatch.setattr(pipeline, "run_qualification", fake_qualification)
    config = pipeline.PearlLemonPipelineConfig(
        raw_output_path=tmp_path / "raw.jsonl",
        normalized_output_path=tmp_path / "normalized.jsonl",
        resolved_output_path=tmp_path / "resolved.jsonl",
        enriched_output_path=tmp_path / "enriched.jsonl",
        qualified_output_path=tmp_path / "qualified.jsonl",
    )

    async def run_both():
        return await asyncio.gather(
            pipeline.run_pipeline(config, frankensurf_client="Alpha"),
            pipeline.run_pipeline(config, frankensurf_client="Beta"),
        )

    results = asyncio.run(run_both())
    run_outputs = []
    for result in results:
        assert result.outputs.qualified_leads is not None
        run_outputs.append(
            json.loads(
                Path(result.outputs.qualified_leads)
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
        )

    assert {record["agency_name"] for record in run_outputs} == {
        "Alpha Company",
        "Beta Company",
    }
    assert len({result.outputs.enriched_records for result in results}) == 2
