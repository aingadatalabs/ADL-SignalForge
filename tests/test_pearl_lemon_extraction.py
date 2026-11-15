import asyncio
import json

import pytest

from agencies.pearl_lemon.extraction.extraction import (
    DEFAULT_RAW_RECORDS_PATH,
    ExtractionRunError,
    PearlLemonExtractionEngine,
    run_extraction,
)
from agencies.pearl_lemon.icp import PearlLemonICP
from agencies.pearl_lemon.normalizer import PearlLemonNormalizer


class FakePage:
    ok = True
    url = "https://example.com/about"
    title = "Example Software"
    text = "Example Software provides workflow tools for business customers."
    receipt = {"final_url": "https://example.com/about"}


class FakeFrankensurf:
    def __init__(self, fail_on_intent_query: bool = False):
        self.fail_on_intent_query = fail_on_intent_query
        self.searched_queries: list[str] = []
        self.crawled_urls: list[str] = []

    def search(self, query: str, *, recency: str):
        self.searched_queries.append(query)
        assert recency == "month"
        if self.fail_on_intent_query and "sales development" in query:
            raise RuntimeError("search unavailable")
        return [
            {
                "title": "Example Software",
                "url": "https://example.com/about",
                "description": "Workflow software for B2B teams.",
                "employee_count": None,
                "technologies": ["HubSpot"],
            },
            {"title": "Result without a company website", "url": "not-a-url"},
        ]

    async def abatch(self, urls: list[str], *, links: bool):
        assert links is True
        self.crawled_urls.extend(urls)
        return [FakePage() for _ in urls]


def small_icp() -> PearlLemonICP:
    return PearlLemonICP(
        target_industries={"B2B SaaS"},
        target_geographies={"United States"},
    )


def test_default_raw_records_path_is_inside_extraction_package():
    assert DEFAULT_RAW_RECORDS_PATH.parent.name == "extraction"
    assert DEFAULT_RAW_RECORDS_PATH.name == "raw_records.jsonl"


def test_search_queries_are_deterministic_and_use_icp_criteria():
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=FakeFrankensurf())

    queries = asyncio.run(engine.generate_search_queries())

    assert queries == [
        'established B2B "B2B SaaS" companies in "United States" 10-250 employees',
        '"B2B SaaS" companies in "United States" "sales development" OR "sales team expansion"',
    ]
    assert all("startup" not in query.casefold() for query in queries)


def test_extraction_uses_observed_frankensurf_interface_and_preserves_unknowns():
    client = FakeFrankensurf()
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=client)

    records = asyncio.run(engine.extract_raw_leads(max_results_per_query=2))

    assert len(records) == 4
    assert client.searched_queries
    assert client.crawled_urls
    record = records[0]
    assert record["company_name"] is None
    assert record["agency_name"] == "Example Software"
    assert record["name_confidence"] == "low"
    assert record["extraction_evidence"][0]["source_type"] == "search_result"
    assert record["industry"] is None
    assert record["country"] is None
    assert record["country_code"] is None
    assert record["business_model"] is None
    assert record["domain"] == "example.com"
    assert record["website_evidence_available"] is True
    assert record["crawl_result"]["pages"][0]["cleaned_text"] == FakePage.text
    assert record["extraction_metadata"]["source_query"]
    assert record["business_activity_evidence"] == "Workflow software for B2B teams."
    assert records[1]["website_evidence_available"] is False


def test_extraction_records_are_consumable_by_shared_normalizer(tmp_path):
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=FakeFrankensurf())
    records = asyncio.run(engine.extract_raw_leads(max_results_per_query=1))

    normalized = PearlLemonNormalizer(
        records,
        output_path=tmp_path / "normalized_records.jsonl",
    ).process()

    assert normalized["stats"]["accepted_count"] == 2
    assert normalized["records"][0]["agency_name"] == "Example Software"
    assert normalized["records"][0]["domain"] == "example.com"
    assert normalized["records"][0]["extraction_evidence"]


def test_partial_query_failure_is_surfaced_with_records_in_exception():
    client = FakeFrankensurf(fail_on_intent_query=True)
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=client)

    with pytest.raises(ExtractionRunError) as error:
        asyncio.run(engine.extract_raw_leads())

    assert len(error.value.failures) == 1
    assert error.value.partial_results
    assert "search unavailable" in error.value.failures[0]["error"]


def test_run_extraction_persists_and_returns_raw_records(tmp_path):
    client = FakeFrankensurf()
    output_path = tmp_path / "extraction" / "raw_records.jsonl"

    records = asyncio.run(
        run_extraction(
            max_results_per_query=1,
            icp=small_icp(),
            client=client,
            output_path=output_path,
        )
    )

    assert len(records) == 2
    assert len(client.searched_queries) == 2
    persisted_records = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted_records == records


def test_run_extraction_persists_partial_records_before_raising(tmp_path):
    output_path = tmp_path / "extraction" / "raw_records.jsonl"

    with pytest.raises(ExtractionRunError) as error:
        asyncio.run(
            run_extraction(
                icp=small_icp(),
                client=FakeFrankensurf(fail_on_intent_query=True),
                output_path=output_path,
            )
        )

    persisted_records = [
        json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert persisted_records == error.value.partial_results


def test_zero_result_limit_is_rejected():
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=FakeFrankensurf())

    with pytest.raises(ValueError, match="at least 1"):
        asyncio.run(engine.extract_raw_leads(max_results_per_query=0))
