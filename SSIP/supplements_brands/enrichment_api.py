import asyncio
import csv
import json
import os
import random
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_DIR = os.path.dirname(__file__)
INPUT_CSV = os.path.join(BASE_DIR, "outputs", "scraped_contact_leads.csv")
OUTPUT_FINAL_CSV = os.path.join(BASE_DIR, "outputs", "final_enriched_outreach_leads.csv")

# Hunter.io API Configuration loaded securely from .env
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"

SEMAPHORE_LIMIT = 3  # Controlled concurrency to keep within Hunter rate limits

# Target departments and executive seniority filters
TARGET_DEPARTMENTS = ["executive", "management", "marketing"]
TARGET_TITLES = [
    "founder", "ceo", "chief executive officer", 
    "owner", "co-founder", "president", 
    "cmo", "head of marketing", "head of ecommerce", "vp of marketing"
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15"
]


def matches_decision_maker(position: str) -> bool:
    """Checks if a person's title matches target executive personas."""
    if not position:
        return False
    pos_lower = position.lower()
    return any(title in pos_lower for title in TARGET_TITLES)


async def enrich_lead_via_hunter(
    client: httpx.AsyncClient, 
    lead: Dict[str, str], 
    semaphore: asyncio.Semaphore
) -> List[Dict[str, Any]]:
    """Queries Hunter.io Domain Search API to identify executive decision-makers."""
    domain = lead.get("domain", "").strip()
    
    if not domain or lead.get("status") != "success":
        return [{
            **lead,
            "decision_maker_name": "",
            "decision_maker_title": "",
            "decision_maker_email": "",
            "decision_maker_confidence": "",
            "decision_maker_linkedin": "",
            "decision_maker_phone": "",
            "enrichment_status": "Skipped / Inactive Domain"
        }]

    params = {
        "domain": domain,
        "api_key": HUNTER_API_KEY,
        "limit": 10  # Pull top 10 contacts to filter locally
    }

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json"
    }

    async with semaphore:
        try:
            await asyncio.sleep(random.uniform(0.3, 0.8))
            response = await client.get(HUNTER_DOMAIN_SEARCH_URL, params=params, headers=headers, timeout=20.0)

            if response.status_code == 429:
                print(f"[WARNING] Hunter API rate limited for {domain}. Backing off...")
                await asyncio.sleep(5.0)
                return [{
                    **lead,
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_email": "",
                    "decision_maker_confidence": "",
                    "decision_maker_linkedin": "",
                    "decision_maker_phone": "",
                    "enrichment_status": "Rate Limited (HTTP 429)"
                }]

            if response.status_code != 200:
                return [{
                    **lead,
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_email": "",
                    "decision_maker_confidence": "",
                    "decision_maker_linkedin": "",
                    "decision_maker_phone": "",
                    "enrichment_status": f"Hunter API Error (HTTP {response.status_code})"
                }]

            data = response.json().get("data", {})
            emails = data.get("emails", [])

            if not emails:
                return [{
                    **lead,
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_email": "",
                    "decision_maker_confidence": "",
                    "decision_maker_linkedin": "",
                    "decision_maker_phone": "",
                    "enrichment_status": "No Decision Maker Found"
                }]

            # Filter for target decision-makers
            decision_makers = [e for e in emails if matches_decision_maker(e.get("position"))]

            # Fallback to executive/management department if title match is empty
            if not decision_makers:
                decision_makers = [
                    e for e in emails 
                    if e.get("department") in TARGET_DEPARTMENTS
                ]

            # Ultimate fallback to top confidence email if no title match
            if not decision_makers:
                decision_makers = emails[:1]

            enriched_rows = []
            for person in decision_makers[:3]:  # Top 3 decision makers per domain
                first_name = person.get("first_name") or ""
                last_name = person.get("last_name") or ""
                full_name = f"{first_name} {last_name}".strip()

                enriched_rows.append({
                    **lead,
                    "decision_maker_name": full_name or "Unknown Executive",
                    "decision_maker_title": person.get("position") or "Executive",
                    "decision_maker_email": person.get("value") or "",
                    "decision_maker_confidence": str(person.get("confidence", "")),
                    "decision_maker_linkedin": person.get("linkedin") or "",
                    "decision_maker_phone": person.get("phone_number") or "",
                    "enrichment_status": "Enriched Success"
                })

            return enriched_rows

        except Exception as e:
            print(f"[ERROR] Exception processing {domain}: {str(e)}")
            return [{
                **lead,
                "decision_maker_name": "",
                "decision_maker_title": "",
                "decision_maker_email": "",
                "decision_maker_confidence": "",
                "decision_maker_linkedin": "",
                "decision_maker_phone": "",
                "enrichment_status": f"Exception: {str(e)}"
            }]


async def run_enrichment_pipeline() -> None:
    if not os.path.exists(INPUT_CSV):
        print(f"[ERROR] Input file missing: {INPUT_CSV}")
        return

    leads: List[Dict[str, str]] = []
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        leads = [row for row in reader]

    print(f"[INFO] Loaded {len(leads)} leads. Executing Hunter.io enrichment...")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

    all_enriched_leads: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(limits=limits) as client:
        if hasattr(asyncio, "TaskGroup"):
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(enrich_lead_via_hunter(client, lead, semaphore)) for lead in leads]
            results = [task.result() for task in tasks]
        else:
            tasks = [enrich_lead_via_hunter(client, lead, semaphore) for lead in leads]
            results = await asyncio.gather(*tasks)

        for res_list in results:
            all_enriched_leads.extend(res_list)

    if not all_enriched_leads:
        print("[ERROR] No enrichment results generated.")
        return

    fieldnames = list(all_enriched_leads[0].keys())
    os.makedirs(os.path.dirname(OUTPUT_FINAL_CSV), exist_ok=True)

    with open(OUTPUT_FINAL_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_enriched_leads)

    print(f"[SUCCESS] Final enriched outreach dataset saved -> {OUTPUT_FINAL_CSV}")


if __name__ == "__main__":
    asyncio.run(run_enrichment_pipeline())