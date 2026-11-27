"""Public API facade for lead enrichment across ADL, agencies, and main entrypoints."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import EnrichmentPolicy, LeadRecord
    from .cache import EnrichmentCache
    from .waterfall import EnrichmentWaterfall, Stage2ProviderCaller, WaterfallRunReport


def _run_coro_sync(coro: Any) -> Any:
    """Execute an async enrichment coroutine safely from sync CLI/scripts or inside an active loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Fallback when invoked from a sync wrapper inside an already-running event loop
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def build_default_waterfall(
    *,
    cache: EnrichmentCache | None = None,
    stage2_caller: Stage2ProviderCaller | None = None,
    enable_web_crawler: bool = True,
    enable_dns_tech: bool = True,
    enable_search_linkedin: bool = True,
    enable_registries: bool = True,
) -> EnrichmentWaterfall:
    """Build a configured EnrichmentWaterfall with Stage 1 fetchers and DuckDB cache."""
    from .cache import EnrichmentCache
    from .providers import build_stage1_fetchers
    from .waterfall import EnrichmentWaterfall

    active_cache = cache or EnrichmentCache()
    fetchers = build_stage1_fetchers(
        enable_web_crawler=enable_web_crawler,
        enable_dns_tech=enable_dns_tech,
        enable_search_linkedin=enable_search_linkedin,
        enable_registries=enable_registries,
    )
    return EnrichmentWaterfall(
        cache=active_cache,
        stage1_fetchers=fetchers,
        stage2_caller=stage2_caller,
    )


async def enrich_lead_async(
    lead: LeadRecord | dict[str, Any],
    *,
    policy: EnrichmentPolicy | None = None,
    waterfall: EnrichmentWaterfall | None = None,
    stage2_caller: Stage2ProviderCaller | None = None,
    force_refresh: bool = False,
    return_report: bool = False,
) -> LeadRecord | WaterfallRunReport:
    """Asynchronously enrich a single lead through Stage 1 and cached Stage 2 fallback."""
    engine = waterfall or build_default_waterfall(stage2_caller=stage2_caller)
    report = await engine.enrich_lead(lead, policy=policy, force_refresh=force_refresh)
    return report if return_report else report.lead


async def enrich_batch_async(
    leads: Iterable[LeadRecord | dict[str, Any]],
    *,
    policy: EnrichmentPolicy | None = None,
    waterfall: EnrichmentWaterfall | None = None,
    stage2_caller: Stage2ProviderCaller | None = None,
    concurrency: int = 5,
    force_refresh: bool = False,
    return_reports: bool = False,
) -> list[LeadRecord] | list[WaterfallRunReport]:
    """Asynchronously enrich a batch of leads with bounded concurrency, preserving input order."""
    lead_items = list(leads)
    if not lead_items:
        return []

    engine = waterfall or build_default_waterfall(stage2_caller=stage2_caller)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded_enrich(item: LeadRecord | dict[str, Any]) -> WaterfallRunReport:
        async with semaphore:
            return await engine.enrich_lead(item, policy=policy, force_refresh=force_refresh)

    reports = await asyncio.gather(*(_bounded_enrich(item) for item in lead_items))
    if return_reports:
        return list(reports)
    return [r.lead for r in reports]


def enrich_lead(
    lead: LeadRecord | dict[str, Any],
    *,
    policy: EnrichmentPolicy | None = None,
    waterfall: EnrichmentWaterfall | None = None,
    stage2_caller: Stage2ProviderCaller | None = None,
    force_refresh: bool = False,
    return_report: bool = False,
) -> LeadRecord | WaterfallRunReport:
    """Synchronously enrich a single lead (for CLI scripts, batch jobs, and agency runners)."""
    return _run_coro_sync(
        enrich_lead_async(
            lead,
            policy=policy,
            waterfall=waterfall,
            stage2_caller=stage2_caller,
            force_refresh=force_refresh,
            return_report=return_report,
        )
    )


def enrich_batch(
    leads: Iterable[LeadRecord | dict[str, Any]],
    *,
    policy: EnrichmentPolicy | None = None,
    waterfall: EnrichmentWaterfall | None = None,
    stage2_caller: Stage2ProviderCaller | None = None,
    concurrency: int = 5,
    force_refresh: bool = False,
    return_reports: bool = False,
) -> list[LeadRecord] | list[WaterfallRunReport]:
    """Synchronously enrich a batch of leads with bounded concurrency, preserving input order."""
    return _run_coro_sync(
        enrich_batch_async(
            leads,
            policy=policy,
            waterfall=waterfall,
            stage2_caller=stage2_caller,
            concurrency=concurrency,
            force_refresh=force_refresh,
            return_reports=return_reports,
        )
    )


def __getattr__(name: str) -> Any:
    """Lazy-export core models so callers can import `from enrichment import LeadRecord, EnrichmentPolicy`."""
    if name in {"ContactRecord", "EnrichmentPolicy", "EnrichmentResult", "LeadRecord", "VerificationStatus"}:
        from . import base

        return getattr(base, name)
    if name == "EnrichmentCache":
        from .cache import EnrichmentCache

        return EnrichmentCache
    if name in {"EnrichmentWaterfall", "WaterfallRunReport"}:
        from . import waterfall

        return getattr(waterfall, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContactRecord",
    "EnrichmentCache",
    "EnrichmentPolicy",
    "EnrichmentResult",
    "EnrichmentWaterfall",
    "LeadRecord",
    "VerificationStatus",
    "WaterfallRunReport",
    "build_default_waterfall",
    "enrich_batch",
    "enrich_batch_async",
    "enrich_lead",
    "enrich_lead_async",
]