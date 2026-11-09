"""
ADL-SignalForge High-Throughput Production Pipeline with JSONL Sink
Path: agencies/adl/run_pipeline.py
"""

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import frankensurf as fs  # type: ignore[reportMissingTypeStubs]

# Ensure repository root is in sys.path for absolute package imports
root_path = Path(__file__).parents[2].resolve()
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from agencies.adl.extraction.extraction_parser import extract_agencies_from_html
from agencies.adl.extraction.normalizers import AgencyNormalizer
from agencies.adl.persistence.sink import JSONLSink

# Configure production structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("adl.signalforge.pipeline")

# Decoupled executors to prevent thread starvation between disk I/O and CPU parsing
_IO_EXECUTOR = ThreadPoolExecutor(max_workers=8)
_PARSING_EXECUTOR = ThreadPoolExecutor(max_workers=4)

# Initialize JSONL persistence sink instance
_JSONL_SINK = JSONLSink("data/output/normalized_agencies.jsonl")


def _is_valid_http_url(url: str) -> bool:
    """Ensure the URL is a fully qualified HTTP/HTTPS endpoint with a valid hostname."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and bool(hostname)
    except Exception:
        return False


def search_agency_targets(query: str, max_results: int = 5) -> List[str]:
    """Executes a resilient search query via Frankensurf with canonical domain deduplication."""
    logger.info("Initiating prospect discovery", extra={"query": query})
    
    try:
        raw_search_response = fs.search(query, recency="month")  # type: ignore[no-untyped-call, misc]
    except Exception as e:
        logger.error("Search execution failed", exc_info=e)
        return []

    if not isinstance(raw_search_response, list):
        logger.error("Unexpected search response type received", extra={"type": str(type(raw_search_response))})
        return []

    valid_urls: List[str] = []
    seen_domains = set()

    for item in raw_search_response:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and _is_valid_http_url(url):
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
            
            if hostname not in seen_domains:
                seen_domains.add(hostname)
                valid_urls.append(url)

        if len(valid_urls) >= max_results:
            break

    logger.info("Extracted targets for crawling", extra={"target_count": len(valid_urls)})
    return valid_urls


async def _read_evidence_file(path: str) -> str:
    """Non-blocking asynchronous wrapper using the dedicated I/O thread pool."""
    def _read() -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return await asyncio.get_running_loop().run_in_executor(_IO_EXECUTOR, _read)


async def persist_records(records: List[Dict[str, Any]]) -> int:
    """Non-blocking asynchronous wrapper to write records to disk using the I/O thread pool."""
    if not records:
        return 0
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_IO_EXECUTOR, _JSONL_SINK.write_records, records)


async def _process_single_page(url: str, page: Any) -> Dict[str, Any]:
    """Concurrently processes an individual crawled page payload."""
    if page is None:
        return {
            "payload": {
                "requested_url": url,
                "crawl_status": "failed",
                "error": "Null page object returned"
            },
            "candidates": []
        }

    is_ok: bool = getattr(page, "ok", False)
    receipt: Dict[str, Any] = getattr(page, "receipt", {}) or {}
    final_url: Optional[str] = receipt.get("final_url") or getattr(page, "url", url)
    method_used: str = receipt.get("method", "Unknown")
    failure_reason: Optional[str] = receipt.get("failure") or getattr(page, "failure", None)

    if not is_ok:
        return {
            "payload": {
                "requested_url": url,
                "final_url": final_url,
                "title": None,
                "content_length": 0,
                "content_usable": False,
                "crawl_method": method_used,
                "crawl_status": "failed",
                "extracted_count": 0,
                "error": failure_reason or "HTTP block or failure"
            },
            "candidates": []
        }

    title: str = getattr(page, "title", "No Title")
    raw_html = ""
    
    evidence_list = receipt.get("evidence", [])
    for ev in evidence_list:
        ev_path = ev.get("path")
        if ev_path:
            try:
                raw_html = await _read_evidence_file(ev_path)
                break
            except Exception:
                pass
    
    if not raw_html:
        raw_html = getattr(page, "markdown", None) or getattr(page, "text", "") or ""

    content_usable = bool(raw_html and len(raw_html.strip()) >= 500)
    page_candidates: List[Dict[str, Any]] = []

    if content_usable:
        loop = asyncio.get_running_loop()
        page_candidates = await loop.run_in_executor(
            _PARSING_EXECUTOR,
            extract_agencies_from_html,
            raw_html,
            final_url or url
        )
        for cand in page_candidates:
            cand["source_url"] = final_url or url

    payload = {
        "requested_url": url,
        "final_url": final_url,
        "title": title,
        "content_length": len(raw_html),
        "content_usable": content_usable,
        "crawl_method": method_used,
        "crawl_status": "success",
        "extracted_count": len(page_candidates),
        "error": None
    }

    return {
        "payload": payload,
        "candidates": page_candidates
    }


async def crawl_and_extract_targets(urls: List[str]) -> Dict[str, Any]:
    """Executes batch crawling, normalization, and asynchronous JSONL disk persistence."""
    if not urls:
        logger.warning("No valid targets provided to crawl.")
        return {"payloads": [], "raw_candidates": [], "normalized_candidates": [], "normalization_stats": {}, "persisted_count": 0}

    try:
        pages: List[Any] = await fs.abatch(urls, links=True)  # type: ignore[no-untyped-call]
    except Exception as e:
        logger.error("Batch crawling execution failed", exc_info=e)
        return {"payloads": [], "raw_candidates": [], "normalized_candidates": [], "normalization_stats": {}, "persisted_count": 0}

    if len(pages) != len(urls):
        logger.error("Crawl result count mismatch", extra={"requested": len(urls), "returned": len(pages)})

    # Execute tasks with exception isolation so a single page failure doesn't crash the batch
    processing_tasks = [_process_single_page(url, page) for url, page in zip(urls, pages)]
    gather_results = await asyncio.gather(*processing_tasks, return_exceptions=True)

    crawled_payloads = []
    all_raw_candidates = []

    for idx, res in enumerate(gather_results):
        if isinstance(res, Exception):
            logger.error("Page processing task raised an exception", exc_info=res)
            url_at_idx = urls[idx] if idx < len(urls) else "unknown"
            crawled_payloads.append({
                "requested_url": url_at_idx,
                "crawl_status": "failed",
                "error": str(res)
            })
        elif isinstance(res, dict):
            crawled_payloads.append(res["payload"])
            all_raw_candidates.extend(res["candidates"])

    # Run Normalization Layer and consume structured return dictionary
    normalizer = AgencyNormalizer(all_raw_candidates)
    normalization_result = normalizer.process()

    normalized_records = normalization_result["records"]
    
    # Persist normalized records to disk sink asynchronously
    persisted_count = await persist_records(normalized_records)

    return {
        "payloads": crawled_payloads,
        "raw_candidates": all_raw_candidates,
        "normalized_candidates": normalized_records,
        "normalization_stats": normalization_result["stats"],
        "persisted_count": persisted_count
    }


async def run_pipeline(query: str = "lead generation marketing agencies digital growth", max_results: int = 5) -> Dict[str, Any]:
    """Orchestrates the end-to-end asynchronous SignalForge telemetry run."""
    urls = search_agency_targets(query, max_results=max_results)
    discovered_count = len(urls)
    
    if not urls:
        return {
            "query": query,
            "status": "no_targets_found",
            "summary": {}
        }
    
    execution_data = await crawl_and_extract_targets(urls)
    payloads = execution_data["payloads"]
    raw_candidates = execution_data["raw_candidates"]
    normalized_candidates = execution_data["normalized_candidates"]
    stats = execution_data["normalization_stats"]
    persisted_count = execution_data["persisted_count"]

    crawl_success = sum(1 for p in payloads if p.get("crawl_status") == "success")
    usable_pages = sum(1 for p in payloads if p.get("content_usable"))
    crawl_failure = len(payloads) - crawl_success

    run_summary = {
        "Search status": "SUCCESS" if discovered_count > 0 else "FAILED",
        "URLs discovered": discovered_count,
        "URLs selected": len(urls),
        "Crawl successful": crawl_success,
        "Crawl failed": crawl_failure,
        "Usable content pages": usable_pages,
        "Raw candidates extracted": len(raw_candidates),
        "Normalized candidates": stats.get("accepted_count", len(normalized_candidates)),
        "Duplicates merged": stats.get("duplicate_count", 0),
        "Records rejected": stats.get("rejected_count", 0),
        "Missing websites retained": stats.get("missing_website_count", 0),
        "Records persisted (JSONL)": persisted_count
    }

    print("\n==========================================")
    print("       SIGNALFORGE RUN SUMMARY            ")
    print("==========================================")
    for key, val in run_summary.items():
        print(f"{key:<28}: {val}")
    print("==========================================\n")

    return {
        "query": query,
        "status": "completed",
        "summary": run_summary,
        "payloads": payloads,
        "normalized_candidates": normalized_candidates
    }


if __name__ == "__main__":
    asyncio.run(run_pipeline())