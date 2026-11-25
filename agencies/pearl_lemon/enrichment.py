"""Evidence-backed Pearl Lemon website and contact enrichment."""

import asyncio
import dns.asyncresolver
import httpx
import ipaddress
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from typing import Final
from urllib.parse import urlsplit

from agencies.adl.enrichment_engine.extractor import extract_agency_intelligence
from agencies.adl.enrichment_engine.schemas import AgencyEnrichmentProfile
from agencies.adl.enrichment_engine.verifier import (
    resolve_domain_mail_infrastructure,
    sanitize_canonical_domain,
    validate_syntax,
)
from agencies.pearl_lemon.dedup import DEFAULT_RESOLVED_RECORDS_PATH
from agencies.pearl_lemon.extraction.extraction import PearlLemonExtractionEngine
from agencies.pearl_lemon.icp import DEFAULT_PEARL_LEMON_ICP, PearlLemonICP

logger = logging.getLogger(__name__)

PEARL_LEMON_ENRICHMENT_FIELD_MAPPINGS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "decision_makers": "decision_makers",
        "contacts": "contacts",
        "social_profiles": "social_profiles",
        "technology_stack": "tech_stack",
        "estimated_team_size": "estimated_team_size",
        "extraction_metadata": "extraction_metadata",
    }
)

DEFAULT_ENRICHED_RECORDS_PATH = (
    Path(__file__).parent / "enrichment_engine" / "enriched_records.jsonl"
)
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
MAX_CRAWL_URLS = 5
MAX_SEARCH_RESULTS = 5
MAX_HUNTER_ATTEMPTS = 3
MAX_EVIDENCE_SNIPPET_LENGTH = 500
CRAWL_PATHS = (
    "/",
    "/about",
    "/about-us",
    "/team",
)
EMAIL_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}",
    re.IGNORECASE,
)
HEADCOUNT_RANGE_PATTERN = re.compile(
    r"(?:\b(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s*(?:\+\s*)?"
    r"(?:employees|team members|team|staff|people)\b"
    r"|\b(?:team of\s+|over\s+|more than\s+)?(\d[\d,]*)\+\s*"
    r"(?:employees|team members|team|staff|people)\b)",
    re.IGNORECASE,
)
HEADCOUNT_EXACT_PATTERN = re.compile(
    r"\b(\d[\d,]*)\s+(?:employees|team members|staff)\b",
    re.IGNORECASE,
)
BUSINESS_MODEL_PATTERN = re.compile(
    r"\bB2B\b|\bbusiness.to.business\b|\bB2C\b|\bbusiness.to.consumer\b",
    re.IGNORECASE,
)
HEADQUARTERS_PATTERN = re.compile(
    r"\b(?:headquartered|headquarters|head office|based)\s+(?:in|at)\s+"
    r"([A-Z][A-Za-z .,'-]{1,70}?)(?=\s*(?:[.;]|$))",
    re.IGNORECASE,
)
COUNTRY_TEXT_PATTERN = re.compile(
    r"\b(United States(?: of America)?|USA|U\.S\.A\.|U\.S\.|"
    r"United Kingdom|Great Britain|England|Scotland|Wales|"
    r"Canada|Australia|Germany|France|Netherlands|Ireland|Switzerland)\b",
    re.IGNORECASE,
)
US_STATE_ZIP_PATTERN = re.compile(
    r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
    r"Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    r"Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|"
    r"Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
    r"North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    r"South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    r"Wisconsin|Wyoming|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|"
    r"MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VT|VA|WA|WV|WI|WY)\s+\d{5}(?:-\d{4})?\b"
)
COUNTRY_TO_ISO2: dict[str, str] = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "us": "US",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "uk": "GB",
    "gb": "GB",
    "canada": "CA",
    "ca": "CA",
    "australia": "AU",
    "au": "AU",
    "germany": "DE",
    "de": "DE",
    "france": "FR",
    "fr": "FR",
    "netherlands": "NL",
    "nl": "NL",
    "ireland": "IE",
    "ie": "IE",
    "switzerland": "CH",
    "ch": "CH",
}
CCTLD_TO_ISO2: tuple[tuple[str, str], ...] = (
    (".co.uk", "GB"),
    (".org.uk", "GB"),
    (".uk", "GB"),
    (".ca", "CA"),
    (".com.au", "AU"),
    (".au", "AU"),
    (".de", "DE"),
    (".fr", "FR"),
    (".nl", "NL"),
    (".ie", "IE"),
    (".ch", "CH"),
)


def _infer_country_code(location: str | None = None, domain: str | None = None) -> str | None:
    if location:
        norm = location.strip().casefold()
        if norm in COUNTRY_TO_ISO2:
            return COUNTRY_TO_ISO2[norm]
        for alias, code in COUNTRY_TO_ISO2.items():
            if len(alias) > 2 and re.search(rf"\b{re.escape(alias)}\b", norm):
                return code
        if US_STATE_ZIP_PATTERN.search(location):
            return "US"
    if domain:
        host = domain.strip().casefold().rstrip(".")
        for suffix, code in CCTLD_TO_ISO2:
            if host.endswith(suffix):
                return code
    return None


class EnrichmentProviderError(RuntimeError):
    """Raised when an external enrichment provider returns unusable data."""


class HunterAPIError(EnrichmentProviderError):
    """Raised for Hunter authentication, response, and request failures."""


class HunterDomainSearchClient:
    """Bounded, authenticated client for Hunter's domain-search endpoint."""

    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
    ) -> None:
        self._api_key = api_key
        self._http_client = http_client
        self._icp = icp

    async def search_domain(self, domain: str) -> dict[str, Any]:
        clean_domain = domain.strip().casefold().removeprefix("www.")
        params = {
            "domain": clean_domain,
            "limit": 10,
        }
        for attempt in range(MAX_HUNTER_ATTEMPTS):
            try:
                response = await self._http_client.get(
                    HUNTER_DOMAIN_SEARCH_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.RequestError as exc:
                if attempt + 1 == MAX_HUNTER_ATTEMPTS:
                    raise HunterAPIError(
                        f"Hunter request failed ({type(exc).__name__})"
                    ) from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == MAX_HUNTER_ATTEMPTS:
                    raise HunterAPIError(
                        f"Hunter returned HTTP {response.status_code} "
                        "after retrying"
                    )
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue
            if response.is_error:
                raise HunterAPIError(
                    f"Hunter returned HTTP {response.status_code}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise HunterAPIError("Hunter returned invalid JSON") from exc
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping) or not isinstance(
                data.get("emails"), list
            ):
                raise HunterAPIError("Hunter response is missing its email results")
            return dict(data)

        raise HunterAPIError("Hunter request failed after retrying")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 5.0)
            except ValueError:
                pass
        return min(0.25 * (2**attempt), 5.0)


class PearlLemonEnrichmentEngine:
    """Enrich resolved companies with ICP-directed web evidence and Hunter contacts."""

    def __init__(
        self,
        *,
        icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
        frankensurf_client: Any | None = None,
        hunter_api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        dns_resolver: Any | None = None,
        max_concurrency: int = 4,
        max_search_results: int = MAX_SEARCH_RESULTS,
    ) -> None:
        api_key = hunter_api_key or os.getenv("HUNTER_API_KEY")
        if not api_key or not api_key.strip():
            raise RuntimeError(
                "Hunter enrichment requires the HUNTER_API_KEY environment variable"
            )
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_search_results < 1:
            raise ValueError("max_search_results must be at least 1")

        self.icp = icp
        self.frankensurf_client = (
            frankensurf_client
            if frankensurf_client is not None
            else PearlLemonExtractionEngine._load_frankensurf()
        )
        self.hunter_api_key = api_key.strip()
        self.http_client = http_client
        self.dns_resolver = dns_resolver
        self.max_concurrency = max_concurrency
        self.max_search_results = max_search_results

    async def enrich_records(
        self, records: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if self.http_client is not None:
            return await self._enrich_with_client(records, self.http_client)

        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as http_client:
            return await self._enrich_with_client(records, http_client)

    async def _enrich_with_client(
        self,
        records: Sequence[dict[str, Any]],
        http_client: httpx.AsyncClient,
    ) -> list[dict[str, Any]]:
        hunter = HunterDomainSearchClient(
            self.hunter_api_key,
            http_client,
            self.icp,
        )
        semaphore = asyncio.Semaphore(self.max_concurrency)
        enriched_records: list[dict[str, Any]] = []
        for batch_start in range(0, len(records), self.max_concurrency):
            batch = records[batch_start : batch_start + self.max_concurrency]
            enriched_records.extend(
                await asyncio.gather(
                    *(
                        self._enrich_one(record, hunter, semaphore)
                        for record in batch
                    )
                )
            )
        return enriched_records

    async def _enrich_one(
        self,
        raw_record: dict[str, Any],
        hunter: HunterDomainSearchClient,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        record = dict(raw_record)
        domain = self._normalize_domain(record)
        errors: list[dict[str, str]] = []
        provider_status: dict[str, str] = {}
        if not domain:
            record["enrichment_status"] = "failed"
            record["enrichment_errors"] = [
                {"provider": "input", "error": "A valid company domain is required"}
            ]
            return record

        company_id = record.get("company_id") or record.get("entity_id")
        record["company_id"] = company_id
        if not record.get("company_name"):
            record["company_name"] = record.get("agency_name")
        record["domain"] = domain
        record["canonical_domain"] = domain
        record.setdefault("website", f"https://{domain}")
        record["company_identity_resolved"] = bool(
            (record.get("entity_id") or record.get("company_name"))
            and self.icp.is_valid_domain(domain)
        )

        search_evidence: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        hunter_data: dict[str, Any] = {"emails": []}
        async with semaphore:
            try:
                search_evidence, pages = await self._collect_web_evidence(
                    record, domain
                )
                provider_status["frankensurf"] = "success"
            except EnrichmentProviderError as exc:
                provider_status["frankensurf"] = "failed"
                errors.append({"provider": "frankensurf", "error": str(exc)})
                logger.exception(
                    "Pearl Lemon Frankensurf enrichment failed",
                    extra={"domain": domain},
                )
            try:
                hunter_data = await hunter.search_domain(domain)
                provider_status["hunter"] = "success"
            except HunterAPIError as exc:
                provider_status["hunter"] = "failed"
                errors.append({"provider": "hunter", "error": str(exc)})
                logger.error(
                    "Pearl Lemon Hunter enrichment failed",
                    extra={"domain": domain, "error": str(exc)},
                )

        self._apply_web_evidence(record, search_evidence, pages)
        hunter_headcount = hunter_data.get("headcount")
        if (
            isinstance(hunter_headcount, str)
            and hunter_headcount.strip()
            and "employee_count" not in record
            and "team_size_text" not in record
        ):
            record["team_size_text"] = hunter_headcount.strip()
        hunter_contacts = self._hunter_contacts(hunter_data)
        website_contacts = self._website_contacts(pages, domain)
        contacts = self._merge_contacts(website_contacts, hunter_contacts)
        mx_status, mx_hosts = await self._resolve_mail(domain, contacts)
        record["contacts"] = self._verify_contacts(
            contacts, domain, mx_status, mx_hosts
        )
        record["contact_channels"] = [
            {
                "value": contact["email"],
                "email": contact["email"],
                "verified": contact["verified"],
                "source_url": contact.get("source_url"),
            }
            for contact in record["contacts"]
        ]
        record["contact_channel_verified"] = any(
            contact["verified"] for contact in record["contacts"]
        )
        record["contact_channel_verification"] = (
            "verified" if record["contact_channel_verified"] else "unverified"
        )
        record["decision_makers"] = self._decision_makers(
            record.get("decision_makers"),
            hunter_contacts,
            record["contacts"],
            domain,
        )
        record["verification_report"] = self._verification_report(
            company_id,
            domain,
            record["decision_makers"],
            record["contacts"],
            mx_status,
            mx_hosts,
        )
        record["website_evidence_available"] = bool(
            record.get("website_evidence_available")
            or any(
                page.get("fetch_status") == "success"
                and page.get("cleaned_text")
                for page in pages
            )
        )
        record["provider_status"] = provider_status
        record["enrichment_errors"] = errors
        record["enrichment_status"] = "partial" if errors else "complete"
        record["icp_enrichment"] = self._icp_summary(record)
        return record

    async def _collect_web_evidence(
        self, record: Mapping[str, Any], domain: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        query = self._build_search_query(record, domain)
        try:
            search_results = await asyncio.to_thread(
                self.frankensurf_client.search,
                query,
                recency="month",
            )
        except Exception as exc:
            raise EnrichmentProviderError(
                f"Frankensurf search failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(search_results, list):
            raise EnrichmentProviderError(
                "Frankensurf search returned a non-list response"
            )

        search_evidence: list[dict[str, Any]] = []
        company_urls: list[str] = []
        seen_urls: set[str] = set()
        for result in search_results[: self.max_search_results]:
            if not isinstance(result, Mapping):
                continue
            source_url = PearlLemonExtractionEngine._result_url(result)
            title = self._string_value(result, "title")
            snippet = self._string_value(result, "description", "snippet")
            if source_url and (title or snippet):
                search_evidence.append(
                    {
                        "source_url": source_url,
                        "source_type": "web_search",
                        "title": title,
                        "text": snippet,
                    }
                )
            if (
                source_url
                and self._same_site(source_url, domain)
                and source_url.casefold() not in seen_urls
                and len(company_urls) < MAX_CRAWL_URLS
            ):
                seen_urls.add(source_url.casefold())
                company_urls.append(source_url)

        direct_urls = [f"https://{domain}{path}" for path in CRAWL_PATHS]
        for url in direct_urls:
            if url.casefold() not in seen_urls and len(company_urls) < MAX_CRAWL_URLS:
                seen_urls.add(url.casefold())
                company_urls.append(url)

        if not company_urls:
            return search_evidence, []
        try:
            pages = await self.frankensurf_client.abatch(
                company_urls,
                links=True,
            )
        except Exception as exc:
            raise EnrichmentProviderError(
                f"Frankensurf crawl failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
            raise EnrichmentProviderError(
                "Frankensurf crawl returned a non-sequence response"
            )

        page_records: list[dict[str, Any]] = []
        for index, source_url in enumerate(company_urls):
            page = pages[index] if index < len(pages) else None
            try:
                page_record = PearlLemonExtractionEngine._page_record(
                    source_url, page
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring malformed Frankensurf page result",
                    extra={"domain": domain, "source_url": source_url},
                )
                continue
            if page_record and self._same_site(page_record.get("url", ""), domain):
                page_records.append(page_record)
        return search_evidence, page_records

    def _build_search_query(
        self, record: Mapping[str, Any], domain: str
    ) -> str:
        company_name = (
            self._string_value(record, "company_name", "agency_name")
            .replace('"', "")
            .strip()
        )
        intent_terms = [
            terms[0]
            for terms in self.icp.buying_intent_signals.values()
            if terms
        ][:6]
        terms = " OR ".join(f'"{term}"' for term in intent_terms)
        if company_name:
            return f'"{company_name}" {domain} ({terms})'
        return f"{domain} ({terms})"

    def _apply_web_evidence(
        self,
        record: dict[str, Any],
        search_evidence: list[dict[str, Any]],
        pages: list[dict[str, Any]],
    ) -> None:
        record["search_evidence"] = search_evidence
        record["crawl_result"] = {
            "company_id": record.get("company_id"),
            "canonical_domain": record.get("canonical_domain"),
            "pages": pages,
        }

        sources = [
            (item["source_url"], f'{item.get("title", "")} {item.get("text", "")}')
            for item in search_evidence
            if item.get("source_url")
        ]
        sources.extend(
            (page["url"], page["cleaned_text"])
            for page in pages
            if page.get("url") and page.get("cleaned_text")
        )
        if not sources:
            return

        tech_evidence: list[dict[str, str]] = []
        observed_technologies = self._string_list(record.get("technologies"))
        for technology in sorted(self.icp.preferred_technologies):
            for source_url, text in sources:
                snippet = self._evidence_for_term(text, technology)
                if snippet:
                    if technology not in observed_technologies:
                        observed_technologies.append(technology)
                    tech_evidence.append(
                        {
                            "name": technology,
                            "source_url": source_url,
                            "evidence": snippet,
                        }
                    )
                    break
        record["technologies"] = observed_technologies
        record["technology_evidence"] = tech_evidence

        intent_evidence: list[dict[str, str]] = []
        intent_terms: list[str] = []
        for category, terms in self.icp.buying_intent_signals.items():
            for term in terms:
                for source_url, text in sources:
                    snippet = self._evidence_for_term(text, term)
                    if snippet:
                        matched_term = self._matched_phrase(term, text)
                        intent_terms.append(matched_term)
                        intent_evidence.append(
                            {
                                "category": category,
                                "term": matched_term,
                                "source_url": source_url,
                                "evidence": snippet,
                            }
                        )
                        break
                if any(item["category"] == category for item in intent_evidence):
                    break
        record["intent_signals"] = list(
            dict.fromkeys(
                [*self._string_list(record.get("intent_signals")), *intent_terms]
            )
        )
        record["intent_signal_evidence"] = intent_evidence
        record["recent_news"] = list(
            dict.fromkeys(
                [
                    *self._string_list(record.get("recent_news")),
                    *[
            item["text"] for item in search_evidence if item.get("text")
                    ],
                ]
            )
        )
        record["job_openings"] = list(
            dict.fromkeys(
                [
                    *self._string_list(record.get("job_openings")),
                    *[
            item["text"]
            for item in search_evidence
            if item.get("text")
            and re.search(
                r"\b(?:hiring|job|career|sales development representative|"
                r"business development manager)\b",
                f'{item.get("title", "")} {item["text"]}',
                re.IGNORECASE,
            )
                    ],
                ]
            )
        )

        full_text = "\n".join(text for _, text in sources)
        if not record.get("industry"):
            matched_industries = self.icp.match_industries([full_text])
            if matched_industries:
                record["industry"] = matched_industries
                record["industry_evidence"] = [
                    {"source_url": source_url, "text": text[:MAX_EVIDENCE_SNIPPET_LENGTH]}
                    for source_url, text in sources
                    if self.icp.match_industries([text])
                ]

        if not record.get("business_model"):
            match = BUSINESS_MODEL_PATTERN.search(full_text)
            if match:
                record["business_model"] = (
                    "B2B" if "B2B" in match.group().upper() else "B2C"
                )
                source_url = next(
                    (
                        url
                        for url, text in sources
                        if BUSINESS_MODEL_PATTERN.search(text)
                    ),
                    None,
                )
                record["business_model_evidence"] = {
                    "source_url": source_url,
                    "text": match.group(),
                }

        if "employee_count" not in record and "team_size_text" not in record:
            for source_url, text in sources:
                range_match = HEADCOUNT_RANGE_PATTERN.search(text)
                if range_match:
                    record["team_size_text"] = range_match.group().strip()
                    record["employee_count_evidence"] = {
                        "source_url": source_url,
                        "text": range_match.group().strip(),
                    }
                    break
            if "team_size_text" not in record:
                for source_url, text in sources:
                    exact_match = HEADCOUNT_EXACT_PATTERN.search(text)
                    if exact_match:
                        record["employee_count"] = int(
                            exact_match.group(1).replace(",", "")
                        )
                        record["employee_count_evidence"] = {
                            "source_url": source_url,
                            "text": exact_match.group().strip(),
                        }
                        break

        domain = str(record.get("canonical_domain") or record.get("domain") or "")
        if not record.get("country_code") and record.get("country"):
            inferred = _infer_country_code(str(record["country"]), domain)
            if inferred:
                record["country_code"] = inferred

        if not record.get("country_code") and not record.get("country"):
            for source_url, text in sources:
                location_match = HEADQUARTERS_PATTERN.search(text)
                if location_match:
                    location = location_match.group(1).strip(" ,.")
                    code = _infer_country_code(location, domain)
                    if code or self.icp.matches_geography([location]):
                        record["headquarters_location"] = location
                        record["country"] = location
                        if code:
                            record["country_code"] = code
                        record["headquarters_evidence"] = {
                            "source_url": source_url,
                            "text": location_match.group(),
                        }
                        break

        if not record.get("country_code"):
            for source_url, text in sources:
                country_match = COUNTRY_TEXT_PATTERN.search(text)
                if country_match:
                    matched_country = country_match.group(1).strip(" ,.")
                    code = _infer_country_code(matched_country, domain)
                    if code:
                        record["country"] = record.get("country") or matched_country
                        record["country_code"] = code
                        record.setdefault(
                            "headquarters_evidence",
                            {"source_url": source_url, "text": country_match.group()},
                        )
                        break
                zip_match = US_STATE_ZIP_PATTERN.search(text)
                if zip_match:
                    record["country"] = record.get("country") or "United States"
                    record["country_code"] = "US"
                    record.setdefault(
                        "headquarters_evidence",
                        {"source_url": source_url, "text": zip_match.group()},
                    )
                    break

        if not record.get("country_code"):
            tld_code = _infer_country_code(None, domain)
            if tld_code:
                record["country_code"] = tld_code

        if not record.get("business_activity_evidence"):
            activity_source = next(
                (
                    (url, text)
                    for url, text in sources
                    if text and len(text.strip()) >= 12
                ),
                None,
            )
            if activity_source:
                record["business_activity_evidence"] = activity_source[1][
                    :MAX_EVIDENCE_SNIPPET_LENGTH
                ]
                record["business_activity_source_url"] = activity_source[0]
                record["business_activity_supported"] = True

        acquisition_needs = self._string_list(record.get("acquisition_needs"))
        service_evidence: list[dict[str, str]] = []
        for category, terms in self.icp.service_alignment_signals.items():
            for term in terms:
                for source_url, text in sources:
                    snippet = self._evidence_for_term(text, term)
                    if snippet:
                        acquisition_needs.append(term)
                        service_evidence.append(
                            {
                                "category": category,
                                "term": term,
                                "source_url": source_url,
                                "evidence": snippet,
                            }
                        )
                        break
                if any(item["category"] == category for item in service_evidence):
                    break
        record["acquisition_needs"] = list(dict.fromkeys(acquisition_needs))
        record["service_alignment_evidence"] = service_evidence

    def _hunter_contacts(self, hunter_data: Mapping[str, Any]) -> list[dict[str, Any]]:
        contacts: list[dict[str, Any]] = []
        for item in hunter_data.get("emails", []):
            if not isinstance(item, Mapping):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            verification = item.get("verification")
            if not isinstance(verification, Mapping):
                verification = {}
            source_urls = self._hunter_source_urls(item.get("sources"))
            full_name = self._hunter_full_name(item)
            contacts.append(
                {
                    "email": value.strip().casefold(),
                    "full_name": full_name,
                    "title": self._string_value(item, "position", "position_raw"),
                    "provenance": item.get("provenance"),
                    "verification_status": verification.get("status"),
                    "decision_maker": item.get("decision_maker") is True,
                    "source_urls": source_urls,
                    "source_type": "hunter_domain_search",
                    "evidence": None,
                }
            )
        return contacts

    def _website_contacts(
        self, pages: list[dict[str, Any]], domain: str
    ) -> list[dict[str, Any]]:
        contacts: list[dict[str, Any]] = []
        for page in pages:
            text = page.get("cleaned_text")
            source_url = page.get("url")
            if not isinstance(text, str) or not isinstance(source_url, str):
                continue
            for email in EMAIL_PATTERN.findall(text):
                normalized_email = email.casefold()
                if self._email_matches_domain(normalized_email, domain):
                    contacts.append(
                        {
                            "email": normalized_email,
                            "full_name": None,
                            "title": None,
                            "provenance": "found",
                            "verification_status": "not_checked",
                            "decision_maker": False,
                            "source_urls": [source_url],
                            "source_type": "website_crawl",
                            "evidence": self._evidence_for_term(
                                text, normalized_email
                            ),
                        }
                    )
        return contacts

    @staticmethod
    def _merge_contacts(
        website_contacts: list[dict[str, Any]],
        hunter_contacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for contact in [*website_contacts, *hunter_contacts]:
            email = contact["email"]
            current = merged.setdefault(email, dict(contact))
            for source_url in contact.get("source_urls", []):
                if source_url not in current["source_urls"]:
                    current["source_urls"].append(source_url)
            for field in ("full_name", "title"):
                if not current.get(field) and contact.get(field):
                    current[field] = contact[field]
            if (
                contact.get("source_type") == "hunter_domain_search"
                and (
                    current.get("source_type") != "hunter_domain_search"
                    or contact.get("verification_status")
                    or current.get("verification_status") in {None, "not_checked"}
                )
            ):
                current["source_type"] = contact["source_type"]
                current["provenance"] = contact.get("provenance")
                current["decision_maker"] = contact.get("decision_maker", False)
                if contact.get("verification_status"):
                    current["verification_status"] = contact["verification_status"]
            elif not current.get("verification_status") and contact.get("verification_status"):
                current["verification_status"] = contact["verification_status"]
        return list(merged.values())

    async def _resolve_mail(
        self, domain: str, contacts: list[dict[str, Any]]
    ) -> tuple[str, list[str]]:
        if not contacts:
            return "invalid_domain", []
        resolver = self.dns_resolver
        if resolver is None:
            resolver = dns.asyncresolver.Resolver()
            resolver.timeout = 3.0
            resolver.lifetime = 3.0
        return await resolve_domain_mail_infrastructure(domain, resolver)

    def _verify_contacts(
        self,
        contacts: list[dict[str, Any]],
        domain: str,
        mail_status: str,
        mx_hosts: list[str],
    ) -> list[dict[str, Any]]:
        verified_contacts: list[dict[str, Any]] = []
        has_mail_route = mail_status in {"mx_present", "implicit_mx"}
        for contact in contacts:
            email = contact["email"]
            syntax_valid = validate_syntax(email)
            domain_matches = self._email_matches_domain(email, domain)
            observed = contact.get("provenance") == "found"
            hunter_verified = (
                contact.get("source_type") == "hunter_domain_search"
                and contact.get("verification_status") == "valid"
            )
            hunter_status_valid = (
                contact.get("source_type") != "hunter_domain_search"
                or contact.get("verification_status") == "valid"
            )
            verified = bool(
                syntax_valid
                and domain_matches
                and (observed or hunter_verified)
                and has_mail_route
                and hunter_status_valid
            )
            source_urls = contact.get("source_urls", [])
            evidence_level = (
                "observed"
                if observed
                else ("hunter_verified" if hunter_verified else "unverified")
            )
            verified_contacts.append(
                {
                    **contact,
                    "source_url": source_urls[0] if source_urls else None,
                    "syntax_valid": syntax_valid,
                    "domain_mail_status": mail_status,
                    "mx_hosts": mx_hosts,
                    "evidence_level": evidence_level,
                    "verified": verified,
                }
            )
        return verified_contacts

    def _decision_makers(
        self,
        existing: Any,
        hunter_contacts: list[dict[str, Any]],
        verified_contacts: list[dict[str, Any]],
        domain: str,
    ) -> list[dict[str, Any]]:
        people: list[dict[str, Any]] = [
            dict(person)
            for person in existing
            if isinstance(person, Mapping)
        ] if isinstance(existing, list) else []
        verified_by_email = {
            contact["email"]: contact
            for contact in verified_contacts
            if contact["verified"]
        }
        seen = {
            (
                str(person.get("full_name", "")).casefold(),
                str(person.get("title", "")).casefold(),
            )
            for person in people
        }
        for contact in hunter_contacts:
            full_name = contact.get("full_name")
            title = contact.get("title")
            source_url = next(
                (
                    url
                    for url in contact.get("source_urls", [])
                    if self._is_http_url(url)
                ),
                None,
            )
            if not isinstance(full_name, str) or not full_name.strip():
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            if not source_url:
                continue
            title_match = self.icp.evaluate(
                {"decision_makers": [{"full_name": full_name, "title": title}]}
            ).matched_job_titles
            if not title_match and contact.get("decision_maker") is not True:
                continue
            key = (full_name.casefold(), title.casefold())
            if key in seen:
                continue
            verified_contact = verified_by_email.get(contact["email"])
            people.append(
                {
                    "full_name": full_name,
                    "title": title,
                    "linkedin_url": None,
                    "corporate_email": (
                        contact["email"] if verified_contact else None
                    ),
                    "source_url": source_url,
                    "evidence": contact.get("evidence"),
                    "confidence": 1.0,
                    "email_evidence_level": (
                        "observed" if verified_contact else "unverified"
                    ),
                }
            )
            seen.add(key)
        return people

    @staticmethod
    def _verification_report(
        company_id: Any,
        domain: str,
        decision_makers: list[dict[str, Any]],
        contacts: list[dict[str, Any]],
        mail_status: str,
        mx_hosts: list[str],
    ) -> dict[str, Any]:
        verified_names = {
            str(person.get("full_name", "")).casefold()
            for person in decision_makers
            if person.get("corporate_email")
        }
        candidates = [
            {
                "full_name": contact.get("full_name") or "",
                "email": contact["email"],
                "permutation_type": contact["source_type"],
                "syntax_valid": contact["syntax_valid"],
                "domain_mail_status": contact["domain_mail_status"],
                "evidence_level": contact["evidence_level"],
                "person_association_status": (
                    "directly_observed"
                    if str(contact.get("full_name") or "").casefold()
                    in verified_names
                    else "not_applicable"
                ),
                "source_reference": contact.get("source_url"),
            }
            for contact in contacts
            if contact["verified"] and contact.get("full_name")
        ]
        return {
            "company_id": company_id,
            "canonical_domain": domain,
            "email_candidates": candidates,
            "domain_mx_hosts": mx_hosts,
            "verification_metadata": {
                "domain_mail_status": mail_status,
                "verified_contact_count": sum(
                    1 for contact in contacts if contact["verified"]
                ),
            },
        }

    def _icp_summary(self, record: Mapping[str, Any]) -> dict[str, Any]:
        assessment = self.icp.evaluate(record)
        return {
            "matched_industries": assessment.matched_industries,
            "matched_job_titles": assessment.matched_job_titles,
            "matched_services": assessment.matched_services,
            "matched_intent_signals": assessment.matched_intent_signals,
            "matched_technologies": assessment.matched_technologies,
            "evidence_gates": assessment.evidence_gates,
        }

    @staticmethod
    def _hunter_full_name(contact: Mapping[str, Any]) -> str | None:
        full_name = contact.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
        parts = [
            value.strip()
            for value in (contact.get("first_name"), contact.get("last_name"))
            if isinstance(value, str) and value.strip()
        ]
        return " ".join(parts) if parts else None

    @classmethod
    def _hunter_source_urls(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(
                source["uri"]
                for source in value
                if isinstance(source, Mapping)
                and isinstance(source.get("uri"), str)
                and cls._is_http_url(source["uri"])
            )
        )

    @staticmethod
    def _email_matches_domain(email: str, domain: str) -> bool:
        _, separator, email_domain = email.rpartition("@")
        return bool(
            separator
            and (
                email_domain.casefold() == domain
                or email_domain.casefold().endswith(f".{domain}")
            )
        )

    @staticmethod
    def _matched_phrase(term: str, text: str) -> str:
        cleaned_term = " ".join(term.split())
        cleaned_text = " ".join(text.split())
        for candidate in (cleaned_term + "s", cleaned_term):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if pattern.search(cleaned_text):
                return candidate.strip()
        return cleaned_term

    @staticmethod
    def _evidence_for_term(text: str, term: str) -> str | None:
        normalized_text = " ".join(text.split())
        normalized_term = " ".join(term.split())
        for candidate in (normalized_term + "s", normalized_term):
            match = re.search(
                rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])",
                normalized_text,
                re.IGNORECASE,
            )
            if match:
                start = max(0, match.start() - 120)
                end = min(len(normalized_text), match.end() + 180)
                return normalized_text[start:end]
        return None

    @staticmethod
    def _string_value(record: Mapping[str, Any], *keys: str) -> str:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip()
            ]
        return []

    @staticmethod
    def _is_http_url(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlsplit(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    @classmethod
    def _same_site(cls, url: str, domain: str) -> bool:
        if not cls._is_http_url(url):
            return False
        host = urlsplit(url).hostname
        return bool(host and (host.casefold() == domain or host.casefold().endswith(f".{domain}")))

    def _normalize_domain(self, record: Mapping[str, Any]) -> str | None:
        candidate = self._string_value(record, "canonical_domain", "domain", "website")
        if not candidate:
            return None
        domain = sanitize_canonical_domain(candidate)
        if not domain or not self.icp.is_valid_domain(domain):
            return None
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            return None
        if domain == "localhost" or domain.endswith((".localhost", ".local", ".internal")):
            return None
        return domain


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in resolved records file {path} "
                    f"at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Expected a JSON object in {path} at line {line_number}"
                )
            records.append(record)
    return records


def _write_jsonl_snapshot(
    records: Sequence[dict[str, Any]], output_path: str | Path
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            for record in records:
                output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def run_enrichment(
    input_path: str | Path = DEFAULT_RESOLVED_RECORDS_PATH,
    output_path: str | Path = DEFAULT_ENRICHED_RECORDS_PATH,
    *,
    records: Sequence[dict[str, Any]] | None = None,
    icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
    frankensurf_client: Any | None = None,
    hunter_api_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    dns_resolver: Any | None = None,
    max_concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Run enrichment, then atomically save the JSONL result snapshot."""
    source_records = list(records) if records is not None else _read_jsonl_records(Path(input_path))
    engine = PearlLemonEnrichmentEngine(
        icp=icp,
        frankensurf_client=frankensurf_client,
        hunter_api_key=hunter_api_key,
        http_client=http_client,
        dns_resolver=dns_resolver,
        max_concurrency=max_concurrency,
    )
    enriched_records = await engine.enrich_records(source_records)
    _write_jsonl_snapshot(enriched_records, output_path)
    return enriched_records


__all__ = [
    "AgencyEnrichmentProfile",
    "DEFAULT_ENRICHED_RECORDS_PATH",
    "EnrichmentProviderError",
    "HunterAPIError",
    "HunterDomainSearchClient",
    "PEARL_LEMON_ENRICHMENT_FIELD_MAPPINGS",
    "PearlLemonEnrichmentEngine",
    "extract_agency_intelligence",
    "run_enrichment",
]
