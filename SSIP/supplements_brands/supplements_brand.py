import asyncio
import json
import logging
import os
import random
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv

# Load environment variables from a local .env file if present
load_dotenv()

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
)
logger = logging.getLogger("SSIP-LeadEngine")

# Controlled concurrency throttle for residential proxy pools
SEMAPHORE_LIMIT = 5

# Oxylabs Residential Proxy Credentials (secured via environment variables)
OXYLABS_HOST = os.getenv("OXYLABS_HOST", "pr.oxylabs.io")
OXYLABS_PORT = os.getenv("OXYLABS_PORT", "7777")
OXYLABS_USER = os.getenv("OXYLABS_USER")
OXYLABS_PASS = os.getenv("OXYLABS_PASS")

# Fail-fast validation for critical proxy secrets with CI/test fallback bypass
if not OXYLABS_USER or not OXYLABS_PASS:
    if os.getenv("CI") == "true" or os.getenv("TOX_ENV") or "pytest" in os.getenv("_", ""):
        OXYLABS_USER = "ci_dummy_user"
        OXYLABS_PASS = "ci_dummy_pass"
    else:
        raise ValueError(
            "Critical security error: OXYLABS_USER and OXYLABS_PASS must be set as environment variables."
        )

# Authenticated proxy endpoint string
PROXY_URL = f"http://{OXYLABS_USER}:{OXYLABS_PASS}@{OXYLABS_HOST}:{OXYLABS_PORT}"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

DEFAULT_TARGET_STORES: List[str] = [
    # Original core supplement competitor set
    "https://www.transparentlabs.com",
    "https://kaged.com",
    "https://ghostlifestyle.com",
    "https://www.cellucor.com",
    "https://legionathletics.com",
    "https://nootropicsdepot.com",
    "https://gorillamind.com",
    "https://pescience.com",
    # Sports nutrition / wellness expansion set
    "https://nutricost.com",
    "https://www.naturemade.com",
    "https://nakednutrition.com",
    "https://www.olly.com",
    "https://www.bulksupplements.com",
    "https://beekeepersnaturals.com",
    "https://www.codeage.com",
    "https://www.livemomentous.com",
    "https://appliednutrition.uk",
    "https://5percentnutrition.com",
    "https://bpisports.com",
    "https://bloomnu.com",
    "https://ancestralsupplements.com",
    "https://apothekary.co",
    "https://shopbeam.com",
    "https://www.bubsnaturals.com",
    "https://greatlakeswellness.com",
    "https://wishgardenherbs.com",
    "https://dripdrop.com",
    "https://www.ultimareplenisher.com",
    "https://londonnootropics.com",
    "https://globalhealing.com",
    "https://www.wildnutrition.com",
    "https://mudwtr.com",
    "https://nutraorganics.com.au",
    "https://jarrow.com",
    "https://manukora.com",
    "https://proteanutrition.com",
    "https://nomadsupplements.store",
    "https://pinnaclesup.com",
    "https://asgardiannutrition.com",
    "https://ahlo.co.uk",
    "https://wassen.com",
    "https://prorganiqnutrition.com",
    "https://tru2u.health",
    "https://cloverhealth.shop",
    "https://wellnessgardensupplements.myshopify.com",
    "https://starsamnaturals.com",
    # Vitamins / supplements / wellness Shopify Plus DTC set
    "https://www.vitalproteins.com",
    "https://ritual.com",
    "https://goli.com",
    "https://www.alaninu.com",
    "https://doublewoodsupplements.com",
    "https://moonjuice.com",
    "https://maryruthorganics.com",
    "https://1upnutrition.com",
    "https://rysesupps.com",
    "https://bpnsupps.com",
    "https://www.promixnutrition.com",
    "https://steelsupplements.com",
    "https://www.ora-organic.com"
]

async def fetch_store_catalog(
    client: httpx.AsyncClient, 
    base_url: str, 
    semaphore: asyncio.Semaphore
) -> Dict[str, Any]:
    """Asynchronously pulls all catalog products via Shopify JSON endpoints through Oxylabs residential proxies."""
    clean_url = base_url.rstrip("/")
    page = 1
    all_products: List[Dict[str, Any]] = []
    
    # Stagger execution start to randomize IP proxy connections
    await asyncio.sleep(random.uniform(0.1, 0.8))

    async with semaphore:
        try:
            while page <= 100:
                headers = {
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": f"{clean_url}/"
                }

                url = f"{clean_url}/products.json?limit=250&page={page}"
                response = await client.get(url, headers=headers, follow_redirects=True, timeout=25.0)
                
                if response.status_code in (429, 430):
                    logger.warning(f"Rate limited on {clean_url}. Skipping store to maintain pipeline velocity.")
                    return {
                        "store": clean_url,
                        "status": "rate_limited",
                        "product_count": len(all_products),
                        "products": all_products
                    }
                    
                if response.status_code != 200:
                    logger.error(f"Store blocked or inactive: {clean_url} (HTTP {response.status_code})")
                    break
                
                try:
                    data = response.json()
                except Exception:
                    logger.error(f"Non-JSON response received from {clean_url} (likely WAF challenge)")
                    break

                products = data.get("products", [])
                if not products:
                    break
                    
                all_products.extend(products)
                if len(products) < 250:
                    break
                    
                page += 1
                await asyncio.sleep(0.5)

            logger.info(f"Successfully ingested {len(all_products)} products from {clean_url}")
            return {
                "store": clean_url, 
                "status": "success", 
                "product_count": len(all_products), 
                "products": all_products
            }

        except Exception as e:
            logger.error(f"Error processing {clean_url}: {str(e)}")
            return {
                "store": clean_url, 
                "status": "error", 
                "message": str(e), 
                "products": []
            }

async def run_lead_engine() -> List[Dict[str, Any]]:
    """Executes concurrent requests across target stores using Oxylabs residential proxies."""
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    
    # Initialize httpx client configured with Oxylabs proxy gateway
    async with httpx.AsyncClient(limits=limits, proxy=PROXY_URL) as client:
        tasks = [
            fetch_store_catalog(client, store, semaphore) 
            for store in DEFAULT_TARGET_STORES
        ]
        return await asyncio.gather(*tasks)

if __name__ == "__main__":
    results = asyncio.run(run_lead_engine())
    
    # Save output dataset to local JSON export
    output_dir = os.path.join(os.path.dirname(__file__), "outputs")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "store_leads.json")
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
        
    logger.info(f"Lead ingestion run complete. Processed {len(results)} targets. Exported data to {output_file}")