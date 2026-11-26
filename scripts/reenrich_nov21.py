import asyncio
import os
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv
from agencies.pearl_lemon.enrichment import run_enrichment
from agencies.pearl_lemon.qualification import (
    PearlLemonQualificationSettings,
    run_qualification,
)

load_dotenv(".env")


async def main() -> None:
    resolved_in = Path(
        "agencies/pearl_lemon/identity_resolution/.runs/pl_20261121T022749Z_2634c622/resolved_records.jsonl"
    )
    enriched_out = Path(
        "agencies/pearl_lemon/enrichment_engine/.runs/pl_20261121T022749Z_2634c622/enriched_records_reenriched.jsonl"
    )
    qualified_out = Path(
        "agencies/pearl_lemon/qualified_leads/.runs/pl_20261121T022749Z_2634c622/qualified_leads_reenriched.jsonl"
    )

    enriched = await run_enrichment(
        input_path=resolved_in,
        output_path=enriched_out,
        hunter_api_key=os.getenv("HUNTER_API_KEY"),
        max_concurrency=4,
    )
    print(f"Re-enriched {len(enriched)} records -> {enriched_out}")

    results = await run_qualification(
        input_path=enriched_out,
        output_path=qualified_out,
        settings=PearlLemonQualificationSettings(),
        max_concurrency=4,
    )
    counts = Counter(r.qualification.status for r in results)
    tiers = Counter(
        r.qualification.tier
        for r in results
        if r.qualification.status == "eligible"
    )
    print("Nov 21 re-enriched qualification breakdown:", dict(counts))
    print("Eligible tiers:", dict(tiers))
    for r in results:
        if r.qualification.status == "eligible":
            print(
                f"  ELIGIBLE: {r.canonical_domain:30s} | tier={r.qualification.tier} | "
                f"score={r.qualification.composite_score} | readiness={r.qualification.outreach_readiness}"
            )


if __name__ == "__main__":
    asyncio.run(main())
