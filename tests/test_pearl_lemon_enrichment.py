import asyncio
import json

import httpx
import pytest

from agencies.pearl_lemon.enrichment import (
    DEFAULT_ENRICHED_RECORDS_PATH,
    HunterDomainSearchClient,
    PearlLemonEnrichmentEngine,
    _infer_country_code,
    run_enrichment,
)


HUNTER_RESPONSE = {
    "data": {
        "domain": "example.com",
        "organization": "Example",
        "emails": [
            {
                "value": "jane@example.com",
                "provenance": "found",
                "verification": {"status": "valid"},
                "first_name": "Jane",
                "last_name": "Doe",
                "position": "Chief Marketing Officer",
                "decision_maker": True,
                "sources": [{"uri": "https://example.com/team"}],
            }
        ],
    }
}


class FakePage:
    ok = True
    markdown = (
        "Acme is a B2B SaaS company with 42 employees. "
        "The team uses HubSpot. Headquarters based in London, United Kingdom. "
        "Sales development representatives are hiring. Contact jane@example.com."
    )

    def __init__(self, url: str):
        self.url = url
        self.receipt = {"final_url": url}


class FakeFrankensurf:
    def __init__(self):
        self.search_queries: list[str] = []
        self.crawled_urls: list[str] = []

    def search(self, query: str, *, recency: str):
        self.search_queries.append(query)
        assert recency == "month"
        return [
            {
                "title": "Sales Development Representative opening",
                "url": "https://example.com/careers",
                "description": (
                    "Example is hiring sales development representatives "
                    "as it expands into new markets."
                ),
            },
            {
                "title": "External result",
                "url": "https://jobs.other-company.test/posting",
                "description": "Unrelated result",
            },
        ]

    async def abatch(self, urls: list[str], *, links: bool):
        assert links is True
        self.crawled_urls.extend(urls)
        return [FakePage(url) for url in urls]


class FakeMX:
    preference = 10
    exchange = "mx.example.com."


class FakeDNSResolver:
    async def resolve(self, domain: str, record_type: str):
        assert domain == "example.com"
        assert record_type == "MX"
        return [FakeMX()]


def test_default_enriched_records_path_is_under_enrichment_engine():
    assert DEFAULT_ENRICHED_RECORDS_PATH.parent.name == "enrichment_engine"
    assert DEFAULT_ENRICHED_RECORDS_PATH.name == "enriched_records.jsonl"


@pytest.mark.parametrize(
    "location",
    ["Austin, TX 78701", "Austin, Texas 78701", "Austin, Texas 78701-1234"],
)
def test_us_state_and_zip_locations_infer_us(location):
    assert _infer_country_code(location) == "US"


@pytest.mark.parametrize(
    ("domain", "country_code"),
    [
        ("example.co.uk", "GB"),
        ("example.ca", "CA"),
        ("example.com.au", "AU"),
    ],
)
def test_country_code_is_inferred_from_common_cctld(domain, country_code):
    assert _infer_country_code(domain=domain) == country_code


def test_hunter_decision_maker_flag_keeps_nonmatching_title():
    engine = PearlLemonEnrichmentEngine(
        hunter_api_key="test-key-only",
        frankensurf_client=FakeFrankensurf(),
    )
    hunter_contacts = engine._hunter_contacts(
        {
            "emails": [
                {
                    "value": "jane@example.com",
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "position": "Office Wizard",
                    "decision_maker": True,
                    "sources": [{"uri": "https://example.com/team"}],
                }
            ]
        }
    )

    assert not engine.icp.evaluate(
        {"decision_makers": [{"full_name": "Jane Doe", "title": "Office Wizard"}]}
    ).matched_job_titles
    decision_makers = engine._decision_makers(
        None, hunter_contacts, [], "example.com"
    )

    assert decision_makers[0]["full_name"] == "Jane Doe"
    assert decision_makers[0]["title"] == "Office Wizard"


def test_enrichment_uses_icp_web_and_hunter_evidence_and_persists(tmp_path):
    requests: list[httpx.Request] = []

    def hunter_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=HUNTER_RESPONSE)

    async def execute() -> list[dict]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(hunter_handler)
        ) as http_client:
            return await run_enrichment(
                records=[
                    {
                        "entity_id": "entity-1",
                        "agency_name": "Acme",
                        "domain": "example.com",
                    }
                ],
                output_path=tmp_path / "enrichment_engine" / "enriched_records.jsonl",
                hunter_api_key="test-key-only",
                frankensurf_client=FakeFrankensurf(),
                http_client=http_client,
                dns_resolver=FakeDNSResolver(),
            )

    enriched = asyncio.run(execute())
    record = enriched[0]
    assert record["company_identity_resolved"] is True
    assert record["website_evidence_available"] is True
    assert record["business_model"] == "B2B"
    assert record["employee_count"] == 42
    assert record["country"] == "London, United Kingdom"
    assert "HubSpot" in record["technologies"]
    assert "sales development representatives" in record["intent_signals"]
    assert record["contacts"][0]["verified"] is True
    assert record["decision_makers"][0]["full_name"] == "Jane Doe"
    assert record["icp_enrichment"]["evidence_gates"][
        "contact_channel_verified_before_outreach"
    ] is True
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer test-key-only"
    assert "api_key" not in requests[0].url.params
    assert dict(requests[0].url.params) == {"domain": "example.com", "limit": "10"}
    output_path = tmp_path / "enrichment_engine" / "enriched_records.jsonl"
    assert json.loads(output_path.read_text(encoding="utf-8")) == record
    assert "test-key-only" not in output_path.read_text(encoding="utf-8")


def test_hunter_client_retries_rate_limited_requests():
    call_count = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests.append(request)
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=HUNTER_RESPONSE)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            hunter = HunterDomainSearchClient("test-key-only", client)
            return await hunter.search_domain(" WWW.Example.com ")

    result = asyncio.run(execute())

    assert call_count == 2
    assert result["domain"] == "example.com"
    assert dict(requests[-1].url.params) == {"domain": "example.com", "limit": "10"}


def test_enrichment_requires_hunter_key(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="HUNTER_API_KEY"):
        PearlLemonEnrichmentEngine(frankensurf_client=FakeFrankensurf())


def test_invalid_domain_is_reported_without_provider_calls():
    record = asyncio.run(
        _enrich_invalid_domain()
    )[0]
    assert record["enrichment_status"] == "failed"
    assert record["enrichment_errors"][0]["provider"] == "input"


async def _enrich_invalid_domain() -> list[dict]:
    engine = PearlLemonEnrichmentEngine(
        hunter_api_key="test-key-only",
        frankensurf_client=FakeFrankensurf(),
    )
    return await engine.enrich_records([{"agency_name": "No domain"}])
