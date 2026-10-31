"""
ADL-SignalForge HTML Content Diagnostic
Path: diagnostic_html.py
"""

import asyncio
import frankensurf as fs  # type: ignore[reportMissingTypeStubs]

async def check_html_extraction() -> None:
    url = "https://influencermarketinghub.com/lead-generation-agencies/"
    pages = await fs.abatch([url], links=True)  # type: ignore[no-untyped-call]
    if pages:
        page = pages[0]
        receipt = getattr(page, "receipt", {})
        evidence_list = receipt.get("evidence", [])
        print("Evidence files:", evidence_list)
        
        # Check if there's an HTML content property or if we can read the evidence path
        for ev in evidence_list:
            path = ev.get("path")
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        sample = f.read(500)
                        print(f"Successfully read evidence file from disk! Sample length: {len(sample)}")
                except Exception as e:
                    print("Could not read evidence file:", e)

if __name__ == "__main__":
    asyncio.run(check_html_extraction())
