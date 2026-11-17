"""Discover and capture raw, evidence-backed Pearl Lemon company candidates.

Extraction preserves observations and provenance; normalization, entity resolution,
and ICP qualification belong to subsequent pipeline stages.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agencies.pearl_lemon.icp import DEFAULT_PEARL_LEMON_ICP, PearlLemonICP

logger = logging.getLogger(__name__)

RawRecord = dict[str, Any]
DEFAULT_RAW_RECORDS_PATH = Path(__file__).with_name("raw_records.jsonl")


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
    ) -> None:
        self.icp = icp
        self.surf_client = client if client is not None else self._load_frankensurf()
        self.failures: list[dict[str, str]] = []

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
        """Build repeatable company-discovery queries from configured ICP criteria."""
        geographies = sorted(self.icp.target_geographies)
        industries = sorted(self.icp.target_industries)
        queries = [
            f'established B2B "{industry}" companies in "{geography}" '
            f"{self.icp.min_team_size}-{self.icp.max_team_size} employees"
            for industry in industries
            for geography in geographies
        ]
        queries.extend(
            f'"{industry}" companies in "{geography}" '
            '"sales development" OR "sales team expansion"'
            for industry in industries
            for geography in geographies
        )
        return queries

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
                    results = await asyncio.to_thread(
                        self.surf_client.search,
                        query,
                        recency="month",
                    )
                    if not isinstance(results, list):
                        raise TypeError(
                            "Frankensurf search returned "
                            f"{type(results).__name__}, expected list"
                        )
                    records, failures = await self._extract_query_results(
                        query, results[:max_results_per_query]
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

    async def _extract_query_results(
        self, query: str, search_results: Sequence[Any]
    ) -> tuple[list[RawRecord], list[dict[str, str]]]:
        failures: list[dict[str, str]] = []
        candidates = [
            (index, result, self._result_url(result))
            for index, result in enumerate(search_results)
            if isinstance(result, Mapping) and self._result_url(result)
        ]
        pages: Sequence[Any] = []
        if candidates:
            urls = [url for _, _, url in candidates]
            try:
                pages = await self.surf_client.abatch(urls, links=True)
                if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
                    raise TypeError(
                        f"Frankensurf crawl returned {type(pages).__name__}, expected a sequence"
                    )
            except Exception as exc:
                failure = {
                    "query": query,
                    "error": f"{type(exc).__name__}: crawl failed: {exc}",
                }
                failures.append(failure)
                logger.exception("Pearl Lemon result crawl failed", extra=failure)
                pages = []

        pages_by_result = {
            result_index: pages[page_index] if page_index < len(pages) else None
            for page_index, (result_index, _, _) in enumerate(candidates)
        }
        urls_by_result = {
            result_index: url for result_index, _, url in candidates
        }
        records = [
            self._build_record(
                query,
                result_index,
                result,
                urls_by_result.get(result_index),
                pages_by_result.get(result_index),
            )
            for result_index, result in enumerate(search_results)
            if isinstance(result, Mapping)
        ]
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
        company_name = cls._first_value(result, "company_name", "name")
        page_title = cls._first_value(result, "title")
        agency_name = company_name or page_title
        industry = cls._first_value(result, "detected_industry", "industry")
        country_code = cls._first_value(result, "country_code")
        country = cls._first_value(result, "country")
        employee_count = result.get("employee_count")
        if not isinstance(employee_count, (int, float, str)) or isinstance(
            employee_count, bool
        ):
            employee_count = None

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
            "company_id": cls._first_value(result, "company_id", "id"),
            "company_name": company_name,
            "agency_name": agency_name,
            "name_confidence": "high" if company_name else "low",
            "domain": cls._first_value(result, "domain", "canonical_domain")
            or cls._domain_from_url(source_url),
            "canonical_domain": cls._first_value(
                result, "canonical_domain", "domain"
            )
            or cls._domain_from_url(source_url),
            "website": cls._first_value(result, "website") or source_url,
            "website_title": page_title,
            "description": description,
            "source_url": source_url,
            "source_urls": [source_url] if source_url else [],
            "extraction_evidence": evidence,
            "industry": industry,
            "country_code": country_code,
            "country": country,
            "employee_count": employee_count,
            "team_size_text": cls._first_value(
                result, "team_size_text", "employee_range", "team_size"
            ),
            "business_model": cls._first_value(result, "business_model", "market_type"),
            "technologies": cls._as_list(
                result.get("technologies") or result.get("tech_stack")
            ),
            "decision_makers": cls._as_list(result.get("decision_makers")),
            "acquisition_needs": cls._as_list(result.get("acquisition_needs")),
            "intent_signals": cls._as_list(result.get("intent_signals")),
            "website_evidence_available": bool(crawled and crawled["cleaned_text"]),
            "crawl_result": {
                "company_id": cls._first_value(result, "company_id", "id"),
                "canonical_domain": cls._first_value(
                    result, "canonical_domain", "domain"
                )
                or cls._domain_from_url(source_url),
                "pages": [crawled] if crawled else [],
            },
            "extraction_metadata": {
                "source_query": query,
                "source_result_index": result_index,
                "source_url": source_url,
                "source_title": page_title,
                "source_description": description,
                "company_name_source": "company_name"
                if company_name
                else "search_result_title_candidate"
                if page_title
                else None,
                "observed_fields": sorted(
                    key
                    for key, value in result.items()
                    if value is not None and value != ""
                ),
            },
        }

        if description and source_url:
            record["business_activity_evidence"] = description
            record["business_activity_source_url"] = source_url
        return record

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
        if not value:
            return None
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if not hostname:
            return None
        hostname = hostname.casefold().rstrip(".")
        return hostname[4:] if hostname.startswith("www.") else hostname

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


async def run_extraction(
    max_results_per_query: int = 10,
    *,
    max_concurrency: int = 4,
    icp: PearlLemonICP = DEFAULT_PEARL_LEMON_ICP,
    client: Any | None = None,
    output_path: str | Path = DEFAULT_RAW_RECORDS_PATH,
    pipeline_context: Mapping[str, Any] | None = None,
) -> list[RawRecord]:
    """Run discovery and append successful raw records to the extraction JSONL file."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    engine = PearlLemonExtractionEngine(icp=icp, client=client)
    try:
        records = await engine.extract_raw_leads(
            max_results_per_query, max_concurrency=max_concurrency
        )
    except ExtractionRunError as exc:
        partial_records = _attach_pipeline_context(
            exc.partial_results, pipeline_context
        )
        _write_raw_records(partial_records, output_path)
        raise ExtractionRunError(exc.failures, partial_records) from exc
    records = _attach_pipeline_context(records, pipeline_context)
    _write_raw_records(records, output_path)
    return records


def _attach_pipeline_context(
    records: Sequence[RawRecord],
    pipeline_context: Mapping[str, Any] | None,
) -> list[RawRecord]:
    if pipeline_context is None:
        return list(records)
    context = dict(pipeline_context)
    return [{**record, "_pipeline_context": context} for record in records]


def _write_raw_records(records: Sequence[RawRecord], output_path: str | Path) -> None:
    """Append raw extraction snapshots without applying normalization."""
    lines = [json.dumps(record, ensure_ascii=False) + "\n" for record in records]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        if lines:
            output_file.writelines(lines)


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
