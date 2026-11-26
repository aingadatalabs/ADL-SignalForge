"""Public API for lead enrichment across ADL, agencies, and main entrypoints."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# Replace `Any` with your concrete Pydantic/dataclass types once finalized
LeadInput = dict[str, Any] | Any
EnrichedLead = dict[str, Any] | Any


def enrich_lead(
    lead: LeadInput,
    *,
    force_refresh: bool = False,
    timeout: float | None = None,
) -> EnrichedLead:
    """Enrich a single lead through the enrichment pipeline."""
    from .waterfall import run_lead_enrichment  # Lazy import avoids import-time cycles/overhead

    return run_lead_enrichment(lead, force_refresh=force_refresh, timeout=timeout)


def enrich_batch(
    leads: Iterable[LeadInput],
    *,
    concurrency: int = 10,
    force_refresh: bool = False,
    fail_fast: bool = False,
) -> list[EnrichedLead]:
    """Enrich a collection of leads and return results aligned with input order."""
    from .waterfall import run_batch_enrichment

    lead_list: Sequence[LeadInput] = list(leads)
    if not lead_list:
        return []
    return run_batch_enrichment(
        lead_list,
        concurrency=concurrency,
        force_refresh=force_refresh,
        fail_fast=fail_fast,
    )


__all__ = [
    "enrich_lead",
    "enrich_batch",
]