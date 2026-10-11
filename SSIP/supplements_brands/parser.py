import csv
import json
import os
from typing import Any, Dict, List, Set
from urllib.parse import urlparse

# Define file paths relative to the script location
BASE_DIR = os.path.dirname(__file__)
INPUT_JSON = os.path.join(BASE_DIR, "outputs", "store_leads.json")
OUTPUT_CSV = os.path.join(BASE_DIR, "outputs", "enrichment_ready_leads.csv")


def extract_clean_domain(url: str) -> str:
    """Extracts a canonical bare domain for downstream enrichment APIs (e.g., transparentlabs.com)."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.lower().strip()


def calculate_lead_score(sku_count: int, avg_price: float) -> int:
    """Computes a GTM ICP lead score based on catalog volume and pricing tier."""
    score = 50  # Base score for valid stores

    # SKU Volume Weighting
    if sku_count >= 200:
        score += 30
    elif sku_count >= 100:
        score += 20
    elif sku_count >= 30:
        score += 10

    # Pricing Level Weighting (Mid-to-High ticket indicates higher budget)
    if avg_price >= 40.0:
        score += 20
    elif avg_price >= 20.0:
        score += 10

    return min(score, 100)


def process_leads_for_enrichment() -> None:
    """Processes raw JSON ingestion outputs, deduplicates domains, computes scores, and exports CSV."""
    if not os.path.exists(INPUT_JSON):
        print(f"[ERROR] Input dataset not found at: {INPUT_JSON}")
        return

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        raw_stores: List[Dict[str, Any]] = json.load(f)

    seen_domains: Set[str] = set()
    enrichment_targets: List[Dict[str, Any]] = []

    for store_data in raw_stores:
        raw_url = store_data.get("store", "")
        status = store_data.get("status", "")
        products = store_data.get("products", [])
        
        if not raw_url:
            continue

        clean_domain = extract_clean_domain(raw_url)

        # 1. Deduplication Guard
        if clean_domain in seen_domains:
            continue
        seen_domains.add(clean_domain)

        # Filter out unreachable domains or empty product catalogs
        if status != "success" or not products:
            enrichment_targets.append({
                "domain": clean_domain,
                "full_store_url": raw_url,
                "lead_score": 0,
                "total_skus": 0,
                "min_price": 0.0,
                "max_price": 0.0,
                "avg_price": 0.0,
                "brand_name": clean_domain.split(".")[0].title(),
                "top_tags": "",
                "status": status,
                "icp_tier": "Unreachable / Blocked"
            })
            continue

        # 2. Extract Catalog Signals
        sku_count = len(products)
        prices: List[float] = []
        tags_set: Set[str] = set()
        vendors_set: Set[str] = set()

        for product in products:
            vendor = product.get("vendor")
            if vendor and str(vendor).strip():
                vendors_set.add(str(vendor).strip())

            prod_tags = product.get("tags", [])
            if isinstance(prod_tags, list):
                tags_set.update([str(t).strip() for t in prod_tags if str(t).strip()])
            elif isinstance(prod_tags, str):
                tags_set.update([t.strip() for t in prod_tags.split(",") if t.strip()])

            for variant in product.get("variants", []):
                try:
                    price_val = float(variant.get("price", 0))
                    if price_val > 0:
                        prices.append(price_val)
                except (ValueError, TypeError):
                    continue

        min_price = round(min(prices), 2) if prices else 0.0
        max_price = round(max(prices), 2) if prices else 0.0
        avg_price = round(sum(prices) / len(prices), 2) if prices else 0.0

        # Primary Brand Name Determination
        primary_brand = list(vendors_set)[0] if vendors_set else clean_domain.split(".")[0].title()

        # 3. Compute ICP Tier & Score
        lead_score = calculate_lead_score(sku_count, avg_price)
        
        if sku_count >= 150:
            icp_tier = "Enterprise Tier 1"
        elif sku_count >= 50:
            icp_tier = "Mid-Market Tier 2"
        else:
            icp_tier = "Boutique / SMB Tier 3"

        enrichment_targets.append({
            "domain": clean_domain,
            "full_store_url": raw_url,
            "lead_score": lead_score,
            "total_skus": sku_count,
            "min_price": min_price,
            "max_price": max_price,
            "avg_price": avg_price,
            "brand_name": primary_brand,
            "top_tags": ", ".join(list(tags_set)[:8]),
            "status": status,
            "icp_tier": icp_tier
        })

    # Sort leads by Lead Score in descending order (Highest priority first)
    enrichment_targets.sort(key=lambda x: x["lead_score"], reverse=True)

    # 4. Export CSV
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    fieldnames = [
        "domain", "full_store_url", "lead_score", "icp_tier", 
        "total_skus", "min_price", "max_price", "avg_price", 
        "brand_name", "top_tags", "status"
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enrichment_targets)

    print(f"[SUCCESS] Processed {len(enrichment_targets)} lead targets. Exported to -> {OUTPUT_CSV}")


if __name__ == "__main__":
    process_leads_for_enrichment()