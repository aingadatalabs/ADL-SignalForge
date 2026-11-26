# Discover and capture raw, evidence-backed Pearl Lemon company candidates.
import asyncio
import json
import logging
import random
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from agencies.pearl_lemon.icp import DEFAULT_PEARL_LEMON_ICP, PearlLemonICP

logger = logging.getLogger(__name__)

RawRecord = dict[str, Any]
MAX_ACTIVITY_EVIDENCE_LENGTH = 500
WESTERN_EUROPE_SEARCH_COUNTRIES = (
    "Austria",
    "Belgium",
    "France",
    "Germany",
    "Ireland",
    "Liechtenstein",
    "Luxembourg",
    "Netherlands",
    "Switzerland",
    "United Kingdom",
)
COUNTRY_CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("united states", "united states of america", "usa"),
    "GB": ("united kingdom", "great britain", "england", "scotland", "wales"),
    "CA": ("canada",),
    "AU": ("australia",),
    "DE": ("germany",),
    "FR": ("france",),
    "NL": ("netherlands",),
    "IE": ("ireland",),
    "CH": ("switzerland",),
}
DEFAULT_RAW_RECORDS_PATH = Path(__file__).with_name("raw_records.jsonl")
BLOCKLISTED_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "britannica.com",
        "f6s.com",
        "builtin.com",
        "builtinlondon.uk",
        "builtinmelbourne.com",
        "clutch.co",
        "g2.com",
        "capterra.com",
        "crunchbase.com",
        "linkedin.com",
        "glassdoor.com",
        "indeed.com",
        "forbes.com",
        "medium.com",
        "revenuebase.ai",
        "uglybaby.app",
        "zoominfo.com",
        "apollo.io",
        "pitchbook.com",
        "tracxn.com",
        "dealroom.co",
        "goodfirms.co",
        "designrush.com",
        "sortlist.com",
        "trustpilot.com",
    }
)


class ExtractionRunError(RuntimeError):
    """Raised for incomplete discovery runs; exposes failures and partial records."""

    def __init__(
        self,
        failures: list[dict[str, str]],
        partial_results: list[RawRecord],
    ) -> None:
        self.failures = failures
        self.partial_results = partial_results
        super().__init__(
            f"Pearl Lemon extraction had {len(failures)} query or crawl failures; "
            f"{len(partial_results)} partial records are available"
        )


class PearlLemonExtractionEngine:
    """Run ICP-derived searches and crawl returned URLs with Frankensurf."""

    def __init__(
        self,
        icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
        client: Any | None = None,
        *,
        serialize_search: bool = True,
        max_search_retries: int = 2,
        search_retry_base_delay: float = 0.5,
    ) -> None:
        if max_search_retries < 0:
            raise ValueError("max_search_retries must be at least 0")
        if search_retry_base_delay < 0:
            raise ValueError("search_retry_base_delay must be at least 0")
        self.icp = icp
        self.surf_client = client if client is not None else self._load_frankensurf()
        self.serialize_search = serialize_search
        self.max_search_retries = max_search_retries
        self.search_retry_base_delay = search_retry_base_delay
        self._search_lock: asyncio.Lock | None = None
        self.failures: list[dict[str, str]] = []

    def _get_search_lock(self) -> asyncio.Lock:
        if self._search_lock is None:
            self._search_lock = asyncio.Lock()
        return self._search_lock

    @staticmethod
    def _load_frankensurf() -> Any:
        try:
            import frankensurf
        except ImportError as exc:
            raise RuntimeError(
                "Frankensurf is required for Pearl Lemon extraction. "
                "Install the project's declared frankensurf dependency."
            ) from exc
        return frankensurf

    async def generate_search_queries(self) -> list[str]:
        """Build bounded, repeatable searches across configured ICP markets."""
        geographies = list(
            dict.fromkeys(
                country
                for geography in sorted(self.icp.target_geographies)
                for country in (
                    WESTERN_EUROPE_SEARCH_COUNTRIES
                    if geography.casefold() == "western europe"
                    else (geography,)
                )
            )
        )
        industries = sorted(self.icp.target_industries)
        if not geographies:
            return []
        geography_clause = (
            f'"{geographies[0]}"'
            if len(geographies) == 1
            else "("
            + " OR ".join(f'"{geography}"' for geography in geographies)
            + ")"
        )
        search_signals = (
            '("about us" OR "our customers" OR "customer stories")',
            '("case studies" OR "client results" OR "headquarters" OR "based in")',
        )
        return [
            f'"{industry}" {geography_clause} {signals} '
            "-top -best -list -directory -wikipedia"
            for industry in industries
            for signals in search_signals
        ]

    async def _invoke_search_with_retry(self, query: str) -> list[Any]:
        attempt = 0
        while True:
            try:
                if self.serialize_search:
                    async with self._get_search_lock():
                        results = await asyncio.to_thread(
                            self.surf_client.search,
                            query,
                        )
                else:
                    results = await asyncio.to_thread(
                        self.surf_client.search,
                        query,
                    )
                if not isinstance(results, list):
                    raise TypeError(
                        "Frankensurf search returned "
                        f"{type(results).__name__}, expected list"
                    )
                return results
            except Exception as exc:
                if attempt >= self.max_search_retries:
                    raise
                delay = self.search_retry_base_delay * (2**attempt) + random.uniform(
                    0.0, 0.25
                )
                logger.warning(
                    "Retrying Frankensurf search after transient error",
                    extra={
                        "query": query,
                        "attempt": attempt + 1,
                        "max_retries": self.max_search_retries,
                        "delay_seconds": round(delay, 3),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                attempt += 1
                await asyncio.sleep(delay)

    async def extract_raw_leads(
        self, max_results_per_query: int = 10, max_concurrency: int = 4
    ) -> list[RawRecord]:
        """Search and crawl ICP-derived targets, preserving source evidence per record."""
        if max_results_per_query < 1:
            raise ValueError("max_results_per_query must be at least 1")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

        self.failures = []
        queries = await self.generate_search_queries()
        raw_leads: list[RawRecord] = []
        logger.info("Starting Pearl Lemon extraction", extra={"query_count": len(queries)})
        semaphore = asyncio.Semaphore(max_concurrency)

        async def extract_query(
            query: str,
        ) -> tuple[list[RawRecord], list[dict[str, str]]]:
            async with semaphore:
                try:
                    results = await self._invoke_search_with_retry(query)
                    records, failures = await self._extract_query_results(
                        query, results, max_results=max_results_per_query
                    )
                    return records, failures
                except Exception as exc:
                    failure = {
                        "query": query,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    logger.exception(
                        "Pearl Lemon discovery query failed", extra=failure
                    )
                    return [], [failure]

        query_results = await asyncio.gather(
            *(extract_query(query) for query in queries)
        )
        for records, failures in query_results:
            raw_leads.extend(records)
            self.failures.extend(failures)

        if self.failures:
            raise ExtractionRunError(
                failures=self.failures,
                partial_results=raw_leads,
            )
        logger.info(
            "Pearl Lemon extraction completed",
            extra={"record_count": len(raw_leads)},
        )
        return raw_leads

    @classmethod
    def _canonical_url_key(cls, url: str | None) -> str | None:
        if not isinstance(url, str):
            return None
        trimmed = url.strip()
        if not trimmed:
            return None
        try:
            parsed = urlsplit(trimmed)
        except ValueError:
            return trimmed
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return trimmed
        host = parsed.hostname.casefold().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        port = parsed.port
        default_port = 80 if parsed.scheme == "http" else 443
        netloc = f"{host}:{port}" if port and port != default_port else host
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))

    @classmethod
    def _page_url_candidates(cls, page: Any) -> list[str]:
        if page is None:
            return []
        candidates: list[str] = []
        if isinstance(page, Mapping):
            receipt = page.get("receipt")
            if isinstance(receipt, Mapping):
                for key in ("final_url", "source_url", "url"):
                    val = receipt.get(key)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val.strip())
            for key in ("url", "source_url", "final_url", "request_url"):
                val = page.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())
            return candidates

        receipt = getattr(page, "receipt", None)
        if isinstance(receipt, Mapping):
            for key in ("final_url", "source_url", "url"):
                val = receipt.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())
        for attr in ("url", "source_url", "final_url", "request_url"):
            val = getattr(page, attr, None)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        return candidates

    @classmethod
    def _index_pages_by_url(
        cls,
        pages: Sequence[Any] | Mapping[Any, Any],
        requested_urls: Sequence[str],
    ) -> dict[str, Any]:
        pages_by_key: dict[str, Any] = {}

        if isinstance(pages, Mapping):
            for raw_key, page_item in pages.items():
                if isinstance(raw_key, str) and raw_key.strip():
                    pages_by_key.setdefault(raw_key.strip(), page_item)
                    canon = cls._canonical_url_key(raw_key)
                    if canon:
                        pages_by_key.setdefault(canon, page_item)
                for candidate_url in cls._page_url_candidates(page_item):
                    pages_by_key.setdefault(candidate_url, page_item)
                    canon = cls._canonical_url_key(candidate_url)
                    if canon:
                        pages_by_key.setdefault(canon, page_item)
            return pages_by_key

        unmatched_pages: list[Any] = []
        matched_requested_indices: set[int] = set()

        for page_item in pages:
            matched_any = False
            for candidate_url in cls._page_url_candidates(page_item):
                pages_by_key.setdefault(candidate_url, page_item)
                canon = cls._canonical_url_key(candidate_url)
                if canon:
                    pages_by_key.setdefault(canon, page_item)
                for req_idx, req_url in enumerate(requested_urls):
                    req_canon = cls._canonical_url_key(req_url)
                    if candidate_url == req_url or (
                        canon and req_canon and canon == req_canon
                    ):
                        matched_requested_indices.add(req_idx)
                        matched_any = True
            if not matched_any:
                unmatched_pages.append(page_item)

        unmatched_req_urls = [
            req_url
            for req_idx, req_url in enumerate(requested_urls)
            if req_idx not in matched_requested_indices
        ]
        if len(pages) == len(requested_urls) and not unmatched_req_urls:
            for req_url, page_item in zip(requested_urls, pages):
                pages_by_key.setdefault(req_url, page_item)
                canon = cls._canonical_url_key(req_url)
                if canon:
                    pages_by_key.setdefault(canon, page_item)
        else:
            for req_url, page_item in zip(unmatched_req_urls, unmatched_pages):
                pages_by_key.setdefault(req_url, page_item)
                canon = cls._canonical_url_key(req_url)
                if canon:
                    pages_by_key.setdefault(canon, page_item)

        return pages_by_key

    @classmethod
    def _lookup_page_for_url(
        cls,
        pages_by_key: Mapping[str, Any],
        source_url: str | None,
    ) -> Any | None:
        if not source_url:
            return None
        if source_url in pages_by_key:
            return pages_by_key[source_url]
        canon = cls._canonical_url_key(source_url)
        if canon and canon in pages_by_key:
            return pages_by_key[canon]
        return None

    async def _extract_query_results(
        self,
        query: str,
        search_results: Sequence[Any],
        *,
        max_results: int,
    ) -> tuple[list[RawRecord], list[dict[str, str]]]:
        failures: list[dict[str, str]] = []
        eligible_results: list[tuple[int, Mapping[str, Any]]] = []
        for original_index, result in enumerate(search_results):
            if not isinstance(result, Mapping):
                continue
            if not self._result_domain(result):
                continue
            if self._is_blocklisted_result(result):
                continue
            eligible_results.append((original_index, result))
            if len(eligible_results) >= max_results:
                break

        candidates: list[tuple[int, Mapping[str, Any], str]] = [
            (original_index, result, source_url)
            for original_index, result in eligible_results
            if (source_url := self._result_url(result))
        ]

        pages_by_key: dict[str, Any] = {}
        if candidates:
            unique_urls = list(
                dict.fromkeys(source_url for _, _, source_url in candidates)
            )
            try:
                raw_pages = await self.surf_client.abatch(unique_urls, links=True)
                if isinstance(raw_pages, Mapping):
                    pages_by_key = self._index_pages_by_url(raw_pages, unique_urls)
                elif isinstance(raw_pages, Sequence) and not isinstance(
                    raw_pages, (str, bytes)
                ):
                    pages_by_key = self._index_pages_by_url(raw_pages, unique_urls)
                else:
                    raise TypeError(
                        f"Frankensurf crawl returned {type(raw_pages).__name__}, "
                        "expected a sequence or mapping"
                    )
            except Exception as exc:
                failure = {
                    "query": query,
                    "error": f"{type(exc).__name__}: crawl failed: {exc}",
                }
                failures.append(failure)
                logger.exception("Pearl Lemon result crawl failed", extra=failure)
                pages_by_key = {}

        records: list[RawRecord] = []
        for original_index, result in eligible_results:
            source_url = self._result_url(result)
            page = self._lookup_page_for_url(pages_by_key, source_url)
            if isinstance(page, BaseException):
                logger.warning(
                    "Pearl Lemon per-URL crawl raised exception",
                    extra={
                        "query": query,
                        "source_result_index": original_index,
                        "source_url": source_url,
                        "error": f"{type(page).__name__}: {page}",
                    },
                )
                page = None

            record = self._build_record(
                query,
                original_index,
                result,
                source_url,
                page,
            )
            final_url = self._first_value(record, "website")
            if self._is_blocklisted_result(result, final_url=final_url):
                logger.info(
                    "Skipping result redirected to a blocklisted domain",
                    extra={
                        "query": query,
                        "source_result_index": original_index,
                        "source_url": source_url,
                        "final_url": final_url,
                    },
                )
                continue
            records.append(record)
        return records, failures

    @classmethod
    def _build_record(
        cls,
        query: str,
        result_index: int,
        result: Mapping[str, Any],
        source_url: str | None,
        page: Any | None,
    ) -> RawRecord:
        crawled = cls._page_record(source_url, page)
        upstream_name = cls._first_value(result, "company_name", "name")
        search_title = cls._first_value(result, "title")
        crawled_title = (
            cls._first_value(crawled, "title") if crawled else None
        )
        page_title = crawled_title or search_title
        crawled_text = (
            cls._first_value(crawled, "cleaned_text") if crawled else None
        )
        crawl_succeeded = bool(
            crawled
            and crawled.get("fetch_status") == "success"
            and crawled_text
        )
        observed_final_url = cls._first_value(crawled, "url") if crawled else None
        final_domain = cls._domain_from_url(observed_final_url)
        source_domain = cls._domain_from_url(source_url)
        declared_domain = cls._result_domain(result)
        company_domain = (
            final_domain or source_domain or declared_domain
        )
        domain_conflict = bool(
            declared_domain
            and company_domain
            and not cls._same_domain_family(declared_domain, company_domain)
        )

        supported_name = None
        name_source = None
        if (
            crawl_succeeded
            and upstream_name
            and cls._company_name_supported(
                upstream_name, crawled_title, crawled_text, company_domain
            )
        ):
            supported_name = upstream_name
            name_source = "verified_result_company_name"
        elif crawl_succeeded and crawled_title and company_domain:
            title_brand = cls._title_brand_candidate(
                crawled_title, company_domain
            )
            if title_brand and cls._text_supports_phrase(title_brand, crawled_text):
                supported_name = title_brand
                name_source = "verified_crawled_title_brand"

        industry_candidate = cls._first_value(
            result, "detected_industry", "industry"
        )
        industry = (
            industry_candidate
            if crawl_succeeded
            and industry_candidate
            and cls._text_supports_phrase(
                industry_candidate,
                f"{crawled_title or ''} {(crawled_text or '')[:1500]}",
            )
            else None
        )
        country_candidate = cls._first_value(result, "country")
        country_code_candidate = cls._first_value(result, "country_code")
        country, country_code = cls._supported_country(
            country_candidate,
            country_code_candidate,
            crawled_text if crawl_succeeded else None,
        )
        employee_count = result.get("employee_count")
        if (
            not isinstance(employee_count, (int, float, str))
            or isinstance(employee_count, bool)
            or not crawl_succeeded
            or not cls._employee_count_supported(employee_count, crawled_text)
        ):
            employee_count = None
        team_size_candidate = cls._first_value(
            result, "team_size_text", "employee_range", "team_size"
        )
        team_size_text = (
            team_size_candidate
            if crawl_succeeded
            and team_size_candidate
            and cls._text_supports_phrase(team_size_candidate, crawled_text)
            else None
        )
        decision_makers = cls._verified_decision_makers(
            result.get("decision_makers"),
            crawled["url"] if crawl_succeeded and crawled else None,
            crawled_text if crawl_succeeded else None,
        )
        raw_decision_makers = cls._as_list(result.get("decision_makers"))
        raw_company_id = cls._first_value(result, "company_id", "id")
        company_id = (
            raw_company_id
            if supported_name and not domain_conflict
            else None
        )

        unverified_fields: list[str] = []
        if upstream_name and not (
            supported_name and name_source == "verified_result_company_name"
        ):
            unverified_fields.append("company_name")
        if industry_candidate and not industry:
            unverified_fields.append("industry")
        if country_candidate and not country:
            unverified_fields.append("country")
        if (
            country_code_candidate
            and country_code_candidate.upper() != country_code
        ):
            unverified_fields.append("country_code")
        if result.get("employee_count") is not None and employee_count is None:
            unverified_fields.append("employee_count")
        if team_size_candidate and not team_size_text:
            unverified_fields.append("team_size_text")
        if len(decision_makers) < len(raw_decision_makers):
            unverified_fields.append("decision_makers")
        if domain_conflict:
            unverified_fields.append("domain")

        description = cls._first_value(result, "description", "snippet")
        evidence: list[dict[str, Any]] = []
        if description and source_url:
            evidence.append(
                {
                    "source_url": source_url,
                    "source_type": "search_result",
                    "field": "description",
                    "text": description,
                }
            )
        if crawled and crawled.get("cleaned_text"):
            evidence.append(
                {
                    "source_url": crawled["url"],
                    "source_type": "website_crawl",
                    "field": "page_text",
                    "text": crawled["cleaned_text"],
                }
            )

        record: RawRecord = {
            "company_id": company_id,
            "company_name": supported_name,
            "agency_name": supported_name,
            "name_confidence": "high" if supported_name else "low",
            "domain": company_domain,
            "canonical_domain": company_domain,
            "website": observed_final_url or source_url,
            "website_title": page_title,
            "description": description,
            "source_url": source_url,
            "source_urls": [source_url] if source_url else [],
            "extraction_evidence": evidence,
            "primary_industry": industry,
            "industry": industry,
            "country_code": country_code,
            "country": country,
            "employee_count": employee_count,
            "team_size_text": team_size_text,
            "business_model": cls._first_value(result, "business_model", "market_type"),
            "technologies": cls._as_list(
                result.get("technologies") or result.get("tech_stack")
            ),
            "decision_makers": decision_makers,
            "acquisition_needs": cls._as_list(result.get("acquisition_needs")),
            "intent_signals": cls._as_list(result.get("intent_signals")),
            "website_evidence_available": bool(crawled and crawled["cleaned_text"]),
            "crawl_result": {
                "company_id": company_id,
                "canonical_domain": company_domain,
                "pages": [crawled] if crawled else [],
            },
            "extraction_metadata": {
                "source_query": query,
                "source_result_index": result_index,
                "source_url": source_url,
                "source_title": search_title,
                "crawled_title": crawled_title,
                "source_description": description,
                "company_name_source": name_source,
                "unverified_fields": sorted(set(unverified_fields)),
                "observed_fields": sorted(
                    key
                    for key, value in result.items()
                    if value is not None and value != ""
                ),
            },
        }

        if (
            crawl_succeeded
            and crawled
            and crawled_text
            and cls._same_site(crawled["url"], company_domain)
        ):
            record["business_activity_evidence"] = crawled_text[
                :MAX_ACTIVITY_EVIDENCE_LENGTH
            ]
            record["business_activity_source_url"] = crawled["url"]
            record["business_activity_source_type"] = "website_crawl"
        return record

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

    @classmethod
    def _text_supports_phrase(cls, value: str, text: str | None) -> bool:
        if not text:
            return False
        phrase = cls._normalize_text(value)
        normalized_text = cls._normalize_text(text)
        return bool(
            len(phrase) >= 3
            and f" {phrase} " in f" {normalized_text} "
        )

    @classmethod
    def _same_domain_family(cls, left: str, right: str) -> bool:
        left_domain = cls._domain_from_url(left) or left.casefold().rstrip(".")
        right_domain = cls._domain_from_url(right) or right.casefold().rstrip(".")
        return (
            left_domain == right_domain
            or left_domain.endswith(f".{right_domain}")
            or right_domain.endswith(f".{left_domain}")
        )

    @classmethod
    def _domain_brand_matches(cls, name: str, domain: str) -> bool:
        labels = cls._domain_from_url(domain) or cls._domain_from_url(
            f"https://{domain}"
        )
        if not labels:
            return False
        ignored_labels = {
            "www", "api", "app", "blog", "co", "com", "net", "org",
            "io", "ai", "uk", "ca", "au", "de", "fr", "nl", "ie", "ch",
        }
        name_tokens = cls._normalize_text(name).split()
        compact_name = "".join(name_tokens)
        return any(
            len(label) >= 3
            and label not in ignored_labels
            and (
                label in name_tokens
                or compact_name.startswith(label)
            )
            for label in labels.split(".")
        )

    @classmethod
    def _title_brand_candidate(cls, title: str, domain: str) -> str | None:
        for segment in re.split(r"\s*(?:\||:|—|–)\s*|\s+-\s+", title):
            candidate = segment.strip(" -|:")
            if candidate and cls._domain_brand_matches(candidate, domain):
                return candidate
        return None

    @classmethod
    def _company_name_supported(
        cls,
        name: str,
        title: str | None,
        text: str | None,
        domain: str | None,
    ) -> bool:
        return bool(
            text
            and domain
            and cls._text_supports_phrase(name, text)
            and (
                (title and cls._text_supports_phrase(name, title))
                or cls._domain_brand_matches(name, domain)
            )
        )

    @classmethod
    def _supported_country(
        cls,
        country: str | None,
        country_code: str | None,
        text: str | None,
    ) -> tuple[str | None, str | None]:
        if not text:
            return None, None
        code = country_code.upper() if country_code else None
        matching_codes = {
            known_code
            for known_code, aliases in COUNTRY_CODE_ALIASES.items()
            if any(cls._text_supports_phrase(alias, text) for alias in aliases)
        }
        if country:
            normalized_country = cls._normalize_text(country)
            country_codes = {
                known_code
                for known_code, aliases in COUNTRY_CODE_ALIASES.items()
                if any(
                    cls._text_supports_phrase(alias, country)
                    for alias in aliases
                )
            }
            supported_codes = country_codes & matching_codes
            if len(supported_codes) == 1:
                supported_code = next(iter(supported_codes))
                return country, supported_code
            if normalized_country in {
                cls._normalize_text(alias)
                for aliases in COUNTRY_CODE_ALIASES.values()
                for alias in aliases
            }:
                return None, None
        if code and code in matching_codes:
            return None, code
        return None, None

    @classmethod
    def _employee_count_supported(cls, value: int | float | str, text: str | None) -> bool:
        if not text:
            return False
        if isinstance(value, (int, float)):
            if (
                isinstance(value, float) and not value.is_integer()
            ) or value < 0:
                return False
            number = int(value)
            spellings = {str(number), f"{number:,}"}
            number_pattern = "|".join(re.escape(item) for item in spellings)
            return bool(
                re.search(
                    rf"(?<!\d)(?:{number_pattern})(?!\d)\s*"
                    r"(?:employees?|team members?|staff|people)\b",
                    text,
                    re.IGNORECASE,
                )
            )
        stripped_value = value.strip()
        if stripped_value.isdigit():
            return cls._employee_count_supported(int(stripped_value), text)
        return cls._text_supports_phrase(stripped_value, text)

    @classmethod
    def _verified_decision_makers(
        cls,
        candidates: Any,
        source_url: str | None,
        text: str | None,
    ) -> list[dict[str, Any]]:
        if not source_url or not text:
            return []
        verified: list[dict[str, Any]] = []
        for candidate in cls._as_list(candidates):
            if not isinstance(candidate, Mapping):
                continue
            candidate_data = cast(Mapping[str, Any], candidate)
            name = cls._first_value(candidate_data, "full_name")
            title = cls._first_value(candidate_data, "title", "job_title")
            if (
                name
                and title
                and cls._text_supports_phrase(name, text)
                and cls._text_supports_phrase(title, text)
            ):
                verified_candidate = dict(candidate_data)
                verified_candidate["source_url"] = source_url
                verified.append(verified_candidate)
        return verified

    @staticmethod
    def _page_record(source_url: str | None, page: Any | None) -> RawRecord | None:
        if not source_url or page is None:
            return None
        ok = bool(getattr(page, "ok", False))
        text = (
            getattr(page, "markdown", None)
            or getattr(page, "text", None)
            or getattr(page, "content", None)
        )
        if not isinstance(text, str):
            text = ""
        receipt = getattr(page, "receipt", {}) or {}
        if not isinstance(receipt, Mapping):
            receipt = {}
        final_url = receipt.get("final_url") or getattr(page, "url", None) or source_url
        return {
            "url": final_url,
            "path": urlsplit(final_url).path or "/",
            "title": getattr(page, "title", None),
            "fetch_status": "success" if ok and text.strip() else "invalid_content",
            "status_code": getattr(page, "status_code", None),
            "content_type": getattr(page, "content_type", None),
            "cleaned_text": text.strip() if ok and text.strip() else None,
            "error": None if ok and text.strip() else (
                receipt.get("failure") or getattr(page, "failure", None)
            ),
            "receipt": dict(receipt),
        }

    @staticmethod
    def _result_url(result: Any) -> str | None:
        if not isinstance(result, Mapping):
            return None
        for key in ("website", "url"):
            value = result.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            parsed = urlsplit(value.strip())
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                return value.strip()
        return None

    @staticmethod
    def _domain_from_url(value: str | None) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = value.strip()
        markdown_link = re.fullmatch(r"\[[^\]]+\]\(([^)]+)\)", candidate)
        if markdown_link:
            candidate = markdown_link.group(1).strip()
        if "://" not in candidate:
            candidate = f"//{candidate.lstrip('/')}"
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        hostname = parsed.hostname
        if not hostname or any(character.isspace() for character in hostname):
            return None
        hostname = hostname.casefold().rstrip(".")
        return hostname[4:] if hostname.startswith("www.") else hostname

    @classmethod
    def _same_site(cls, url: str, domain: str | None) -> bool:
        source_domain = cls._domain_from_url(url)
        if not source_domain or not domain:
            return False
        return source_domain == domain or source_domain.endswith(f".{domain}")

    @classmethod
    def _result_domain(cls, result: Mapping[str, Any]) -> str | None:
        for key in ("domain", "canonical_domain"):
            value = cls._first_value(result, key)
            if value:
                domain = cls._domain_from_url(value)
                if domain:
                    return domain
        return cls._domain_from_url(cls._result_url(result))

    @classmethod
    def _is_blocklisted_result(
        cls, result: Mapping[str, Any], final_url: str | None = None
    ) -> bool:
        domains = [
            cls._domain_from_url(final_url),
            cls._domain_from_url(cls._result_url(result)),
            cls._result_domain_field(result, "domain"),
            cls._result_domain_field(result, "canonical_domain"),
        ]
        return any(
            domain
            and any(
                domain == blocked_domain or domain.endswith(f".{blocked_domain}")
                for blocked_domain in BLOCKLISTED_DOMAINS
            )
            for domain in domains
        )

    @classmethod
    def _result_domain_field(
        cls, result: Mapping[str, Any], field: str
    ) -> str | None:
        value = cls._first_value(result, field)
        if not value:
            return None
        return cls._domain_from_url(value)

    @staticmethod
    def _first_value(result: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return []


def _record_dedup_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    metadata = record.get("extraction_metadata")
    meta_map = metadata if isinstance(metadata, Mapping) else {}
    raw_query = meta_map.get("source_query")
    query = raw_query.strip() if isinstance(raw_query, str) else ""

    raw_domain = (
        record.get("canonical_domain")
        or record.get("domain")
        or meta_map.get("source_url")
        or record.get("source_url")
    )
    domain = (
        PearlLemonExtractionEngine._domain_from_url(raw_domain)
        if isinstance(raw_domain, str)
        else None
    ) or ""

    raw_source_url = meta_map.get("source_url") or record.get("source_url")
    canonical_source_url = (
        PearlLemonExtractionEngine._canonical_url_key(raw_source_url)
        if isinstance(raw_source_url, str)
        else None
    ) or (raw_source_url.strip() if isinstance(raw_source_url, str) else "")

    return (query, domain, canonical_source_url)


def _load_existing_dedup_keys(path: Path) -> set[tuple[str, str, str]]:
    seen_keys: set[tuple[str, str, str]] = set()
    if not path.exists():
        return seen_keys
    with path.open("r", encoding="utf-8") as existing_file:
        for line_number, line in enumerate(existing_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed JSONL line while loading deduplication keys",
                    extra={"path": str(path), "line_number": line_number},
                )
                continue
            if isinstance(parsed, Mapping):
                seen_keys.add(_record_dedup_key(parsed))
    return seen_keys


async def run_extraction(
    max_results_per_query: int = 10,
    *,
    max_concurrency: int = 4,
    icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
    client: Any | None = None,
    output_path: str | Path = DEFAULT_RAW_RECORDS_PATH,
    pipeline_context: Mapping[str, Any] | None = None,
    serialize_search: bool = True,
    max_search_retries: int = 2,
    search_retry_base_delay: float = 0.5,
) -> list[RawRecord]:
    """Run discovery and append deduplicated raw records to the extraction JSONL file."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    run_id = uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    engine = PearlLemonExtractionEngine(
        icp=icp,
        client=client,
        serialize_search=serialize_search,
        max_search_retries=max_search_retries,
        search_retry_base_delay=search_retry_base_delay,
    )
    try:
        records = await engine.extract_raw_leads(
            max_results_per_query, max_concurrency=max_concurrency
        )
    except ExtractionRunError as exc:
        partial_records = _attach_pipeline_context(
            exc.partial_results, pipeline_context
        )
        _write_raw_records(
            partial_records,
            output_path,
            run_id=run_id,
            extracted_at=started_at,
        )
        raise ExtractionRunError(exc.failures, partial_records) from exc
    records = _attach_pipeline_context(records, pipeline_context)
    _write_raw_records(
        records,
        output_path,
        run_id=run_id,
        extracted_at=started_at,
    )
    return records


def _attach_pipeline_context(
    records: Sequence[RawRecord],
    pipeline_context: Mapping[str, Any] | None,
) -> list[RawRecord]:
    if pipeline_context is None:
        return list(records)
    context = dict(pipeline_context)
    return [{**record, "_pipeline_context": context} for record in records]


def _write_raw_records(
    records: Sequence[RawRecord],
    output_path: str | Path,
    *,
    run_id: str | None = None,
    extracted_at: str | None = None,
) -> None:
    """Append deduplicated raw extraction snapshots with run metadata."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_keys = _load_existing_dedup_keys(path)
    effective_run_id = run_id or uuid4().hex
    effective_timestamp = extracted_at or datetime.now(timezone.utc).isoformat()

    lines: list[str] = []
    skipped_duplicates = 0
    for record in records:
        key = _record_dedup_key(record)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        stamped_record: RawRecord = {
            **record,
            "_run_metadata": {
                "run_id": effective_run_id,
                "extracted_at_utc": effective_timestamp,
                "output_path": str(path),
                "dedup_key": {
                    "source_query": key[0],
                    "domain": key[1],
                    "canonical_source_url": key[2],
                },
            },
        }
        lines.append(json.dumps(stamped_record, ensure_ascii=False) + "\n")

    with path.open("a", encoding="utf-8") as output_file:
        if lines:
            output_file.writelines(lines)

    logger.info(
        "Persisted raw extraction records",
        extra={
            "run_id": effective_run_id,
            "output_path": str(path),
            "written_count": len(lines),
            "skipped_duplicates": skipped_duplicates,
        },
    )


__all__ = [
    "ExtractionRunError",
    "DEFAULT_RAW_RECORDS_PATH",
    "PearlLemonExtractionEngine",
    "RawRecord",
    "run_extraction",
]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_extraction())