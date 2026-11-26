import asyncio
import json
import threading
import time

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
        self.search_options: list[dict[str, str]] = []
        self.crawled_urls: list[str] = []

    def search(self, query: str, **options: str):
        self.searched_queries.append(query)
        self.search_options.append(options)
        if self.fail_on_intent_query and '"case studies"' in query:
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
        (
            '"B2B SaaS" "United States" '
            '("about us" OR "our customers" OR "customer stories") '
            "-top -best -list -directory -wikipedia"
        ),
        (
            '"B2B SaaS" "United States" '
            '("case studies" OR "client results" OR "headquarters" OR "based in") '
            "-top -best -list -directory -wikipedia"
        ),
    ]
    assert all("startup" not in query.casefold() for query in queries)


def test_default_queries_avoid_industry_geography_cartesian_fanout():
    engine = PearlLemonExtractionEngine(client=FakeFrankensurf())

    queries = asyncio.run(engine.generate_search_queries())

    assert len(queries) == 16
    assert all('"Western Europe"' not in query for query in queries)
    assert all('"France"' in query and '"Germany"' in query for query in queries)
    assert all(
        "(solutions OR product OR platform OR customers OR services)" not in query
        for query in queries
    )


def test_extraction_uses_observed_frankensurf_interface_and_preserves_unknowns():
    client = FakeFrankensurf()
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=client)

    records = asyncio.run(engine.extract_raw_leads(max_results_per_query=2))

    assert len(records) == 2
    assert client.searched_queries
    assert client.search_options == [{}, {}]
    assert client.crawled_urls == ["https://example.com/about"] * 2
    record = records[0]
    assert record["company_name"] == "Example Software"
    assert record["agency_name"] == "Example Software"
    assert record["name_confidence"] == "high"
    assert record["extraction_metadata"]["company_name_source"] == (
        "verified_crawled_title_brand"
    )
    assert record["extraction_evidence"][0]["source_type"] == "search_result"
    assert record["primary_industry"] is None
    assert record["industry"] is None
    assert record["country"] is None
    assert record["country_code"] is None
    assert record["business_model"] is None
    assert record["domain"] == "example.com"
    assert record["website_evidence_available"] is True
    assert record["crawl_result"]["pages"][0]["cleaned_text"] == FakePage.text
    assert record["extraction_metadata"]["source_query"]
    assert record["business_activity_evidence"] == FakePage.text
    assert record["business_activity_evidence"] != "Workflow software for B2B teams."
    assert record["business_activity_source_url"] == FakePage.url
    assert record["business_activity_source_type"] == "website_crawl"


def test_search_snippet_alone_does_not_become_business_activity_evidence():
    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={
            "title": "Example Software",
            "url": "https://example.com/about",
            "description": "Workflow software for B2B teams.",
        },
        source_url="https://example.com/about",
        page=None,
    )

    assert "business_activity_evidence" not in record
    assert "business_activity_source_url" not in record


def test_extraction_does_not_trust_industry_without_crawl_evidence():
    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={
            "company_name": "Example Software",
            "url": "https://example.com/about",
            "detected_industry": "Enterprise Software",
        },
        source_url="https://example.com/about",
        page=None,
    )

    assert record["primary_industry"] is None
    assert record["industry"] is None
    assert "industry" in record["extraction_metadata"]["unverified_fields"]


def test_extraction_uses_domain_brand_in_crawled_title_instead_of_full_title():
    class PricingPage:
        ok = True
        url = "https://acme.com/pricing"
        title = "Pricing - Acme | Enterprise Cloud"
        text = "Acme helps enterprise teams manage cloud operations."
        receipt = {"final_url": "https://acme.com/pricing"}

    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={"title": PricingPage.title, "url": PricingPage.url},
        source_url=PricingPage.url,
        page=PricingPage(),
    )

    assert record["company_name"] == "Acme"
    assert record["agency_name"] == "Acme"
    assert record["name_confidence"] == "high"


def test_extraction_does_not_use_page_title_as_name_when_brand_conflicts_with_domain():
    class MisleadingTitlePage:
        ok = True
        url = "https://example.com/pricing"
        title = "Pricing - Acme | Enterprise Cloud"
        text = "Example Software provides cloud software for enterprises."
        receipt = {"final_url": "https://example.com/pricing"}

    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={"title": MisleadingTitlePage.title, "url": MisleadingTitlePage.url},
        source_url=MisleadingTitlePage.url,
        page=MisleadingTitlePage(),
    )

    assert record["company_name"] is None
    assert record["agency_name"] is None
    assert record["name_confidence"] == "low"
    assert record["website_title"] == MisleadingTitlePage.title


def test_extraction_rejects_unverified_structured_fields_and_domain():
    class ContradictoryPage:
        ok = True
        url = "https://acme.com/pricing"
        title = "Pricing - Acme | Enterprise Cloud"
        text = (
            "Acme helps enterprise teams manage cloud operations. "
            "Headquartered in the United States."
        )
        receipt = {"final_url": "https://acme.com/pricing"}

    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={
            "company_id": "unverified-id",
            "company_name": "Other Corporation",
            "domain": "other.example",
            "title": "Pricing - Acme | Enterprise Cloud",
            "url": "https://acme.com/pricing",
            "detected_industry": "E-commerce",
            "country_code": "GB",
            "country": "United Kingdom",
            "employee_count": 300,
            "decision_makers": [
                {
                    "full_name": "Jamie Example",
                    "title": "CEO",
                    "source_url": "https://other.example/team",
                }
            ],
        },
        source_url="https://acme.com/pricing",
        page=ContradictoryPage(),
    )

    assert record["domain"] == "acme.com"
    assert record["company_id"] is None
    assert record["company_name"] == "Acme"
    assert record["primary_industry"] is None
    assert record["country"] is None
    assert record["country_code"] is None
    assert record["employee_count"] is None
    assert record["decision_makers"] == []
    assert set(record["extraction_metadata"]["unverified_fields"]) == {
        "company_name",
        "country",
        "country_code",
        "decision_makers",
        "domain",
        "employee_count",
        "industry",
    }


def test_extraction_preserves_structured_fields_supported_by_the_crawl():
    class VerifiedPage:
        ok = True
        url = "https://acme.com/about"
        title = "Acme"
        text = (
            "Acme is an E-commerce company headquartered in the United Kingdom. "
            "Acme employs 50 employees. Jamie Example is the CEO."
        )
        receipt = {"final_url": "https://acme.com/about"}

    record = PearlLemonExtractionEngine._build_record(
        query="E-commerce companies",
        result_index=0,
        result={
            "company_id": "acme-id",
            "company_name": "Acme",
            "url": VerifiedPage.url,
            "detected_industry": "E-commerce",
            "country": "United Kingdom",
            "country_code": "GB",
            "employee_count": 50,
            "decision_makers": [
                {
                    "full_name": "Jamie Example",
                    "title": "CEO",
                    "source_url": VerifiedPage.url,
                }
            ],
        },
        source_url=VerifiedPage.url,
        page=VerifiedPage(),
    )

    assert record["company_name"] == "Acme"
    assert record["primary_industry"] == "E-commerce"
    assert record["country"] == "United Kingdom"
    assert record["country_code"] == "GB"
    assert record["employee_count"] == 50
    assert record["decision_makers"][0]["source_url"] == VerifiedPage.url


def test_extraction_skips_directory_and_domainless_results_before_crawling():
    class MixedResultsClient:
        def __init__(self):
            self.crawled_urls: list[str] = []

        def search(self, _query: str):
            return [
                {
                    "title": "Directory profile",
                    "url": "https://company.revenuebase.ai/profile/acme",
                },
                {"title": "Directory profile", "url": "https://www.clutch.co/profile/acme"},
                {"title": "No company domain", "url": "not-a-url"},
                {"title": "Acme", "url": "https://acme.example/about"},
                {"title": "Later company", "url": "https://later.example/about"},
            ]

        async def abatch(self, urls: list[str], *, links: bool):
            assert links is True
            self.crawled_urls.extend(urls)
            class MatchingPage(FakePage):
                def __init__(self, url: str):
                    self.url = url
                    self.receipt = {"final_url": url}

            return [MatchingPage(url) for url in urls]

    client = MixedResultsClient()
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=client)

    records = asyncio.run(engine.extract_raw_leads(max_results_per_query=1))

    assert len(records) == 2
    assert all(record["domain"] == "acme.example" for record in records)
    assert client.crawled_urls == ["https://acme.example/about"] * 2


@pytest.mark.parametrize(
    ("value", "expected_domain"),
    [
        ("example.com/path", "example.com"),
        ("https://example.com/path", "example.com"),
        (
            "[example.com/path](https://example.com/path)",
            "example.com",
        ),
    ],
)
def test_result_domain_helpers_normalize_domain_formats_consistently(
    value, expected_domain
):
    result = {"domain": value}

    assert PearlLemonExtractionEngine._result_domain(result) == expected_domain
    assert (
        PearlLemonExtractionEngine._result_domain_field(result, "domain")
        == expected_domain
    )


def test_markdown_domain_field_is_checked_against_blocklist():
    result = {
        "domain": "[wikipedia.org/wiki](https://wikipedia.org/wiki)",
        "url": "https://example.com/about",
    }

    assert PearlLemonExtractionEngine._result_domain(result) == "wikipedia.org"
    assert PearlLemonExtractionEngine._is_blocklisted_result(result)


@pytest.mark.parametrize(
    "final_url",
    [
        "https://www.wikipedia.org/wiki/Acme",
        "https://www.linkedin.com/company/acme",
    ],
)
@pytest.mark.parametrize("page_ok", [True, False])
def test_extraction_skips_results_redirected_to_blocklisted_domains(
    final_url, page_ok
):
    class RedirectedPage:
        ok = page_ok
        url = "https://example.com/about"
        title = "Acme"
        text = "Acme provides business software."
        receipt = {"final_url": final_url}

    class RedirectingClient:
        async def abatch(self, _urls: list[str], *, links: bool):
            assert links is True
            return [RedirectedPage()]

    engine = PearlLemonExtractionEngine(
        icp=small_icp(),
        client=RedirectingClient(),
    )
    result, failures = asyncio.run(
        engine._extract_query_results(
            "B2B SaaS companies",
            [{"title": "Acme", "url": "https://example.com/about"}],
            max_results=1,
        )
    )

    assert result == []
    assert failures == []


def test_extraction_uses_final_redirect_domain_even_when_page_content_fails():
    class FailedRedirectPage:
        ok = False
        url = "https://example.com/about"
        text = ""
        receipt = {"final_url": "https://acme.com/about"}

    record = PearlLemonExtractionEngine._build_record(
        query="B2B SaaS companies",
        result_index=0,
        result={
            "company_name": "Acme",
            "domain": "example.com",
            "url": "https://example.com/about",
        },
        source_url="https://example.com/about",
        page=FailedRedirectPage(),
    )

    assert record["domain"] == "acme.com"
    assert record["canonical_domain"] == "acme.com"
    assert record["website"] == "https://acme.com/about"


def test_extraction_runs_queries_with_a_bounded_concurrency():
    class ConcurrentSearchClient:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def search(self, _query: str, **options: str):
            assert options == {}
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return []

    client = ConcurrentSearchClient()
    engine = PearlLemonExtractionEngine(icp=small_icp(), client=client)

    records = asyncio.run(
        engine.extract_raw_leads(max_results_per_query=1, max_concurrency=2)
    )

    assert records == []
    assert client.max_active == 2


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


def test_run_extraction_persists_pipeline_context_with_raw_records(
    tmp_path,
    monkeypatch,
):
    async def fake_extract_raw_leads(
        self, _max_results_per_query, *, max_concurrency
    ):
        assert max_concurrency == 4
        return [{"agency_name": "Example Software", "domain": "example.com"}]

    monkeypatch.setattr(
        PearlLemonExtractionEngine,
        "extract_raw_leads",
        fake_extract_raw_leads,
    )
    output_path = tmp_path / "raw_records.jsonl"

    records = asyncio.run(
        run_extraction(
            icp=small_icp(),
            client=object(),
            output_path=output_path,
            pipeline_context={"run_id": "test-run", "agency": "pearl_lemon"},
        )
    )

    persisted = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert records[0]["_pipeline_context"]["run_id"] == "test-run"
    assert persisted["_pipeline_context"]["agency"] == "pearl_lemon"


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


def test_run_extraction_creates_empty_snapshot_when_no_records_are_found(
    tmp_path,
    monkeypatch,
):
    async def no_records(self, _max_results_per_query, *, max_concurrency):
        return []

    monkeypatch.setattr(PearlLemonExtractionEngine, "extract_raw_leads", no_records)
    output_path = tmp_path / "extraction" / "raw_records.jsonl"

    records = asyncio.run(
        run_extraction(
            icp=small_icp(),
            client=object(),
            output_path=output_path,
        )
    )

    assert records == []
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == ""


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
