import asyncio
import logging
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlparse
import frankensurf as fs  # type: ignore[reportMissingTypeStubs]

# Configure production structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("adl.signalforge.crawler")


def _is_valid_http_url(url: str) -> bool:
    """Ensure the URL is a fully qualified HTTP/HTTPS endpoint."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


async def discover_and_qualify_agencies() -> None:
    query: str = "lead generation marketing agencies digital growth"
    logger.info("Initiating prospect discovery", extra={"query": query})
    
    # 1. Resilient Search Execution
    try:
        raw_search_response = fs.search(query, recency="month")  # type: ignore[no-untyped-call, misc]
        search_results: List[Dict[str, Any]] = cast(List[Dict[str, Any]], raw_search_response)
    except Exception as e:
        logger.error("Search execution failed", exc_info=e)
        return

    # 2. Extract and validate URLs (Type is statically known, no isinstance needed)
    raw_urls: List[Optional[str]] = [
        item.get("url") for item in search_results
    ]
    
    urls_to_crawl: List[str] = [
        u for u in raw_urls 
        if u is not None and _is_valid_http_url(u)
    ][:5]
    
    logger.info("Extracted targets for crawling", extra={"target_count": len(urls_to_crawl)})

    if not urls_to_crawl:
        logger.warning("No valid targets found to crawl.")
        return

    # 3. Resilient Batch Crawling Execution
    try:
        pages: List[Any] = await fs.abatch(urls_to_crawl, links=True)  # type: ignore[no-untyped-call]
    except Exception as e:
        logger.error("Batch crawling execution failed", exc_info=e)
        return

    # 4. Process Crawl Results safely
    for url, page in zip(urls_to_crawl, pages):
        if page is None:
            logger.warning("Received null page object for URL", extra={"url": url})
            continue

        is_ok: bool = getattr(page, "ok", False)
        if is_ok:
            title: str = getattr(page, "title", "No Title")
            page_text: str = getattr(page, "text", "") or ""
            receipt: Dict[str, Any] = getattr(page, "receipt", {}) or {}
            method_used: str = receipt.get("method", "Unknown")

            logger.info(
                "Successfully crawled target",
                extra={
                    "url": url,
                    "title": title,
                    "method": method_used,
                    "snippet_length": len(page_text),
                },
            )
            print(f"\n[SUCCESS] Crawled: {url}")
            print(f"Title: {title}")
            print(f"Snippet: {page_text[:300]}...")
            print(f"Tool Used (Receipt): {method_used}")
        else:
            failure_reason: str = getattr(page, "failure", "Unknown error")
            logger.warning(
                "Crawl target blocked or failed",
                extra={"url": url, "reason": failure_reason},
            )
            print(f"\n[BLOCKED/FAILED] {url} -> Reason: {failure_reason}")


if __name__ == "__main__":
    asyncio.run(discover_and_qualify_agencies())