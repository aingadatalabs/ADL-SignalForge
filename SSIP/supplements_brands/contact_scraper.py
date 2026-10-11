import asyncio
import csv
import os
import random
import re
from typing import Any, Dict, List, Set
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_DIR = os.path.dirname(__file__)
INPUT_CSV = os.path.join(BASE_DIR, "outputs", "enrichment_ready_leads.csv")
OUTPUT_CONTACTS_CSV = os.path.join(BASE_DIR, "outputs", "scraped_contact_leads.csv")

SEMAPHORE_LIMIT = 4

# Load Oxylabs Residential Proxy Credentials Securely from Environment
OXYLABS_HOST = os.getenv("OXYLABS_HOST", "pr.oxylabs.io")
OXYLABS_PORT = os.getenv("OXYLABS_PORT", "7777")
OXYLABS_USER = os.getenv("OXYLABS_USER", "")
OXYLABS_PASS = os.getenv("OXYLABS_PASS", "")

PROXY_URL = f"http://{OXYLABS_USER}:{OXYLABS_PASS}@{OXYLABS_HOST}:{OXYLABS_PORT}"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
PHONE_REGEX = re.compile(r'\(?\b[2-9][0-9]{2}\)?[-. ]?[2-9][0-9]{2}[-. ]?[0-9]{4}\b')


def extract_social_links(soup: BeautifulSoup) -> Dict[str, str]:
    """Extracts social media profile links found in site footers and headers safely."""
    socials = {"instagram": "", "linkedin": "", "facebook": "", "twitter": "", "tiktok": ""}
    for a in soup.find_all("a", href=True):
        raw_href = a.get("href")
        if not raw_href:
            continue
        href = str(raw_href).lower()
        if "instagram.com/" in href and not socials["instagram"]:
            socials["instagram"] = str(raw_href)
        elif "linkedin.com/" in href and not socials["linkedin"]:
            socials["linkedin"] = str(raw_href)
        elif "facebook.com/" in href and not socials["facebook"]:
            socials["facebook"] = str(raw_href)
        elif ("twitter.com/" in href or "x.com/" in href) and not socials["twitter"]:
            socials["twitter"] = str(raw_href)
        elif "tiktok.com/" in href and not socials["tiktok"]:
            socials["tiktok"] = str(raw_href)
    return socials


async def scrape_store_contacts(
    client: httpx.AsyncClient, 
    lead: Dict[str, str], 
    semaphore: asyncio.Semaphore
) -> Dict[str, Any]:
    """Crawls storefront homepage and contact/about pages for contact info."""
    base_url = lead.get("full_store_url", "").rstrip("/")
    if not base_url or lead.get("status") != "success":
        return {**lead, "contact_emails": "", "phone_numbers": "", "instagram": "", "linkedin": "", "facebook": "", "twitter": "", "tiktok": ""}

    found_emails: Set[str] = set()
    found_phones: Set[str] = set()
    socials: Dict[str, str] = {"instagram": "", "linkedin": "", "facebook": "", "twitter": "", "tiktok": ""}

    pages_to_crawl = [base_url, f"{base_url}/pages/contact", f"{base_url}/pages/about-us", f"{base_url}/pages/contact-us"]

    async with semaphore:
        for page_url in pages_to_crawl:
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{base_url}/"
            }
            try:
                response = await client.get(page_url, headers=headers, follow_redirects=True, timeout=15.0)
                if response.status_code != 200:
                    continue

                html_text = response.text
                soup = BeautifulSoup(html_text, "html.parser")

                # Extract Mailto Links
                for mailto in soup.select('a[href^="mailto:"]'):
                    raw_mailto = mailto.get("href")
                    if raw_mailto:
                        email = str(raw_mailto).replace("mailto:", "").split("?")[0].strip()
                        if EMAIL_REGEX.match(email):
                            found_emails.add(email.lower())

                # Regex Search on Raw Text
                raw_emails = EMAIL_REGEX.findall(html_text)
                for email in raw_emails:
                    clean_e = email.lower().strip()
                    if not clean_e.endswith(('.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', 'sentry.io')):
                        found_emails.add(clean_e)

                # Extract Phone Numbers
                raw_phones = PHONE_REGEX.findall(html_text)
                for phone in raw_phones:
                    found_phones.add(phone.strip())

                # Extract Social Links
                extracted_socials = extract_social_links(soup)
                for k, v in extracted_socials.items():
                    if v and not socials[k]:
                        socials[k] = v

            except Exception:
                continue

    return {
        **lead,
        "contact_emails": ", ".join(list(found_emails)[:5]),
        "phone_numbers": ", ".join(list(found_phones)[:3]),
        "instagram": socials["instagram"],
        "linkedin": socials["linkedin"],
        "facebook": socials["facebook"],
        "twitter": socials["twitter"],
        "tiktok": socials["tiktok"]
    }


async def run_contact_scraper() -> None:
    if not os.path.exists(INPUT_CSV):
        print(f"[ERROR] Input file missing: {INPUT_CSV}")
        return

    leads: List[Dict[str, str]] = []
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        leads = [row for row in reader]

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)

    async with httpx.AsyncClient(limits=limits, proxy=PROXY_URL) as client:
        if hasattr(asyncio, "TaskGroup"):
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(scrape_store_contacts(client, lead, semaphore)) for lead in leads]
            results = [task.result() for task in tasks]
        else:
            tasks = [scrape_store_contacts(client, lead, semaphore) for lead in leads]
            results = await asyncio.gather(*tasks)

    fieldnames = list(results[0].keys()) if results else []
    os.makedirs(os.path.dirname(OUTPUT_CONTACTS_CSV), exist_ok=True)

    with open(OUTPUT_CONTACTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"[SUCCESS] Scraped contact details for {len(results)} lead targets -> {OUTPUT_CONTACTS_CSV}")


if __name__ == "__main__":
    asyncio.run(run_contact_scraper())