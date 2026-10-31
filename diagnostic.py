"""
ADL-SignalForge FrankenSurf Interface Diagnostic
Path: diagnostic.py
"""

import asyncio
import frankensurf as fs  # type: ignore[reportMissingTypeStubs]

async def inspect_frankensurf() -> None:
    url = "https://influencermarketinghub.com/top-lead-generation-agencies/"
    
    print("=== 1. SEARCH RESPONSE INSPECTION ===")
    try:
        search_result = fs.search(
            "lead generation marketing agencies digital growth",
            recency="month",
        )
        print("Search Result Type:", type(search_result))
        print("Search Result Sample:", repr(search_result)[:2000])
    except Exception as e:
        print("Search diagnostic failed:", e)

    print("\n=== 2. BATCH CRAWL RESPONSE INSPECTION ===")
    try:
        pages = await fs.abatch([url], links=True)  # type: ignore[no-untyped-call]
        print("Pages List Type:", type(pages))
        print("Pages Count:", len(pages))
        
        if pages:
            page = pages[0]
            print("\nPage Object Type:", type(page))
            print("Available Attributes/Methods:", dir(page))
            
            for attribute in (
                "ok", "url", "title", "text",
                "html", "content", "links",
                "receipt", "failure",
            ):
                value = getattr(page, attribute, None)
                print(f"\n--- Attribute: [{attribute}] ---")
                print(repr(value)[:1200])
    except Exception as e:
        print("Batch crawl diagnostic failed:", e)

if __name__ == "__main__":
    asyncio.run(inspect_frankensurf())
