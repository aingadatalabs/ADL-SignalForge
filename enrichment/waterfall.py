"""Waterfall orchestrator: Stage 1 public/web/LinkedIn extraction -> Cache/API fallback."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .base import (
    ContactRecord,
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    merge_lead,
    normalize_domain,
)
from .cache import EnrichmentCache, FallbackAttempt

Stage1Fetcher = Callable[[LeadRecord, EnrichmentPolicy], Awaitable[list[EnrichmentResult]]]
Stage2ProviderCaller = Callable[[str, str, LeadRecord, EnrichmentPolicy], Awaitable[EnrichmentResult | None]]
SufficiencyChecker = Callable[[LeadRecord, EnrichmentPolicy], bool]


@dataclass
class WaterfallRunReport:
    """Audit trail of how a lead was resolved across Stage 1, Cache, and Stage 2."""

    lead: LeadRecord
    stage_1_results: int = 0
    used_cache_or_api_fallback: bool = False
    fallback_attempts: list[FallbackAttempt] = field(default_factory=list)
    stopped_early_zero_credits: bool = False
    notes: list[str] = field(default_factory=list)


def default_is_sufficient(lead: LeadRecord, policy: EnrichmentPolicy) -> bool:
    """Return True if core company + decision-maker contact requirements are already met."""
    has_company_identity = bool(lead.domain and lead.company_name)
    if not has_company_identity:
        return False

    if not lead.contacts:
        return False

    target_titles_lower = [t.strip().lower() for t in policy.target_titles if t.strip()]
    for contact in lead.contacts:
        title_matches = (
            not target_titles_lower
            or (contact.title and any(t in contact.title.lower() for t in target_titles_lower))
        )
        has_contact_point = bool(contact.email or contact.linkedin_url or contact.phone)
        email_ok = (
            not policy.verify_emails
            or contact.verified_status in {VerificationStatus.VALID, VerificationStatus.CATCH_ALL}
            or bool(contact.linkedin_url)
        )
        if title_matches and has_contact_point and email_ok:
            return True

    return False


class EnrichmentWaterfall:
    """Coordinates Stage 1 public/web/LinkedIn extraction and Stage 2 cached API fallback."""

    def __init__(
        self,
        *,
        cache: EnrichmentCache | None = None,
        stage1_fetchers: Iterable[Stage1Fetcher] | None = None,
        stage2_caller: Stage2ProviderCaller | None = None,
        sufficiency_checker: SufficiencyChecker = default_is_sufficient,
    ) -> None:
        self.cache = cache or EnrichmentCache()
        self.stage1_fetchers: list[Stage1Fetcher] = list(stage1_fetchers or [])
        self.stage2_caller = stage2_caller
        self.sufficiency_checker = sufficiency_checker

    def register_stage1_fetcher(self, fetcher: Stage1Fetcher) -> None:
        self.stage1_fetchers.append(fetcher)

    async def enrich_lead(
        self,
        lead: LeadRecord | dict[str, Any],
        policy: EnrichmentPolicy | None = None,
        *,
        force_refresh: bool = False,
    ) -> WaterfallRunReport:
        active_policy = policy or EnrichmentPolicy()
        current = lead if isinstance(lead, LeadRecord) else LeadRecord.model_validate(lead)
        if current.domain:
            current.domain = normalize_domain(current.domain)

        report = WaterfallRunReport(lead=current)

        # --- Stage 1: Zero-cost first-party web, LinkedIn/public search, DNS/tech, registries ---
        for fetcher in self.stage1_fetchers:
            try:
                results = await fetcher(current, active_policy)
            except Exception as exc:
                report.notes.append(f"stage1_fetcher_error:{type(exc).__name__}:{exc}")
                continue

            for res in results:
                if res is None:
                    continue
                # Stage 1 is zero-credit by design unless explicitly tagged
                stage1_res = res.model_copy(
                    update={"stage": res.stage or "stage_1", "credits_used": 0.0}
                )
                current = merge_lead(current, stage1_res, active_policy, stage="stage_1")
                report.stage_1_results += 1

        # --- Gate: Stop before spending API credits if Stage 1 already answered enough ---
        if not force_refresh and self.sufficiency_checker(current, active_policy):
            report.stopped_early_zero_credits = True
            report.lead = current
            return report

        # --- Stage 2: Cache lookup + ordered .env API fallback for remaining gaps ---
        if not active_policy.allow_stage_2_escalation or self.stage2_caller is None:
            report.lead = current
            return report

        if current.credits_used >= active_policy.max_credits_per_lead:
            report.notes.append("lead_credit_cap_reached_before_stage2")
            report.lead = current
            return report

        report.used_cache_or_api_fallback = True
        primary_contact = current.contacts[0] if current.contacts else ContactRecord()

        async def _invoke_provider(provider: str, api_key: str) -> EnrichmentResult | None:
            assert self.stage2_caller is not None
            return await self.stage2_caller(provider, api_key, current, active_policy)

        fallback_result, attempts = await self.cache.resolve_with_fallback(
            operation="waterfall_enrich",
            call_provider=_invoke_provider,
            domain=current.domain,
            linkedin_url=primary_contact.linkedin_url,
            person_name=primary_contact.full_name,
            email=primary_contact.email,
            force_refresh=force_refresh,
        )
        report.fallback_attempts.extend(attempts)

        if fallback_result is not None:
            remaining_lead_budget = max(0.0, active_policy.max_credits_per_lead - current.credits_used)
            capped_cost = min(fallback_result.credits_used, remaining_lead_budget)
            stage2_res = fallback_result.model_copy(
                update={"stage": "stage_2", "credits_used": capped_cost}
            )
            current = merge_lead(current, stage2_res, active_policy, stage="stage_2")

        report.lead = current
        return report

    async def enrich_batch(
        self,
        leads: Iterable[LeadRecord | dict[str, Any]],
        policy: EnrichmentPolicy | None = None,
        *,
        force_refresh: bool = False,
    ) -> list[WaterfallRunReport]:
        reports: list[WaterfallRunReport] = []
        for item in leads:
            reports.append(
                await self.enrich_lead(item, policy=policy, force_refresh=force_refresh)
            )
        return reports


__all__ = [
    "EnrichmentWaterfall",
    "Stage1Fetcher",
    "Stage2ProviderCaller",
    "SufficiencyChecker",
    "WaterfallRunReport",
    "default_is_sufficient",
]