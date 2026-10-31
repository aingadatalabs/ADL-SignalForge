"""
ADL-SignalForge Live Integration Test Suite
Path: test_signalforge.py
"""

import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Any, cast

# Ensure root path is in sys.path for absolute imports
root_path = Path(__file__).parent.resolve()
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

import frankensurf as fs  # type: ignore[reportMissingTypeStubs]
from ADL.run_pipeline import _is_valid_http_url
from ADL.extraction.extraction_parser import extract_agencies_from_html
from ADL.extraction.normalizers import AgencyNormalizer

async def run_live_pipeline_test() -> None:
    print("=== STARTING ADL-SIGNALFORGE LIVE PIPELINE & EXTRACTION TEST ===")
    
    query = "top lead generation agencies digital growth"
    print(f"\n[1] Executing live Frankensurf search for query: '{query}'")
    
    try:
        raw_search_response = fs.search(query, recency="month")  # type: ignore[no-untyped-call, misc]
        search_results: List[Dict[str, Any]] = cast(List[Dict[str, Any]], raw_search_response)
    except Exception as e:
        print(f"❌ Search execution failed: {e}")
        return

    raw_urls: List[str | None] = [item.get("url") for item in search_results]
    urls_to_crawl: List[str] = [
        u for u in raw_urls if u is not None and _is_valid_http_url(u)
    ][:3] # Test top 3 live targets

    print(f"-> Found {len(urls_to_crawl)} valid live target URLs to crawl.")

    if not urls_to_crawl:
        print("❌ No valid URLs to crawl.")
        return

    print(f"\n[2] Executing Frankensurf batch crawl (abatch)...")
    try:
        pages: List[Any] = await fs.abatch(urls_to_crawl, links=True)  # type: ignore[no-untyped-call]
    except Exception as e:
        print(f"❌ Batch crawl failed: {e}")
        return

    all_raw_candidates: List[Dict[str, Any]] = []

    for url, page in zip(urls_to_crawl, pages):
        if page is None or not getattr(page, "ok", False):
            continue
            
        html_content: str = getattr(page, "text", "") or ""
        method_used: str = getattr(page, "receipt", {}).get("method", "Unknown")
        print(f"-> Crawled {url} via [{method_used}] ({len(html_content)} bytes)")

        # Run extraction parser on live HTML content
        candidates = extract_agencies_from_html(html_content, url)
        print(f"   Extracted {len(candidates)} raw agency candidates from this page.")
        all_raw_candidates.extend(candidates)

    print(f"\n[3] Total raw candidates extracted across all live pages: {len(all_raw_candidates)}")

    # Run Normalization Layer
    print("\n[4] Running AgencyNormalizer record cleaning and deduplication...")
    normalizer = AgencyNormalizer(all_raw_candidates)
    normalized_candidates: List[Dict[str, Any]] = normalizer.process()

    print(f"\n[5] Final Processed & Deduplicated Candidate Records ({len(normalized_candidates)} entries):")
    for idx, candidate in enumerate(normalized_candidates, 1):
        print(f"\n--- Candidate {idx} ---")
        print(f"  ID:          {candidate.get('agency_id')}")
        print(f"  Name:        {candidate.get('agency_name')}")
        print(f"  Domain:      {candidate.get('domain')}")
        print(f"  Website:     {candidate.get('website')}")
        print(f"  Confidence:  {candidate.get('confidence')}")
        desc = str(candidate.get('description', ''))
        print(f"  Description: {desc[:100]}...")
        print(f"  Evidence:    {candidate.get('extraction_evidence')}")

    print("\n=== LIVE PIPELINE INTEGRATION TEST COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(run_live_pipeline_test())