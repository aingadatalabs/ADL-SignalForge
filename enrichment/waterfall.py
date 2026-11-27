"""Waterfall orchestrator: Stage 1 public/web/LinkedIn extraction -> Cache/API fallback."""

from __future__ import annotations

import asyncio
import re
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

Stage1Fetcher = Callable[
    [LeadRecord, EnrichmentPolicy],
    Awaitable[list[EnrichmentResult] | Iterable[EnrichmentResult | None] | EnrichmentResult | None],
]
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


def _title_matches_target(title: str | None, target_titles: list[str]) -> bool:
    """Match target role titles on word/token boundaries rather than arbitrary substrings."""
    if not target_titles:
        return True
    if not title or not title.strip():
        return False

    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if not normalized_title:
        return False

    for raw_target in target_titles:
        normalized_target = re.sub(r"[^a-z0-9]+", " ", raw_target.lower()).strip()
        if not normalized_target:
            continue
        pattern = r"(?:^|\b)" + r"\s+".join(re.escape(tok) for tok in normalized_target.split()) + r"(?:\b|$)"
        if re.search(pattern, normalized_title):
            return True

    return False


def _select_stage2_contact(lead: LeadRecord, policy: EnrichmentPolicy) -> ContactRecord:
    """Pick the best contact for Stage 2 lookup: title match first, then key identifiers."""
    if not lead.contacts:
        return ContactRecord()

    target_titles = [t.strip() for t in policy.target_titles if t.strip()]

    def _candidate_rank(contact: ContactRecord) -> tuple[int, int, int, int, float]:
        title_match = 1 if _title_matches_target(contact.title, target_titles) else 0
        key_identifiers = sum(
            1
            for val in (
                contact.email,
                contact.linkedin_url,
                contact.full_name,
            )
            if val and str(val).strip()
        )
        secondary_fields = sum(
            1
            for val in (
                contact.first_name,
                contact.last_name,
                contact.title,
                contact.phone,
            )
            if val and str(val).strip()
        )
        verified_bonus = 1 if contact.verified_status == VerificationStatus.VALID else 0
        return (title_match, key_identifiers, verified_bonus, secondary_fields, contact.confidence)

    return max(lead.contacts, key=_candidate_rank)


def default_is_sufficient(lead: LeadRecord, policy: EnrichmentPolicy) -> bool:
    """Return True if core company + decision-maker contact requirements are already met."""
    has_company_identity = bool(lead.domain and lead.company_name)
    if not has_company_identity:
        return False

    if not lead.contacts:
        return False

    target_titles = [t.strip() for t in policy.target_titles if t.strip()]
    for contact in lead.contacts:
        title_matches = _title_matches_target(contact.title, target_titles)
        has_contact_point = bool(contact.email or contact.linkedin_url or contact.phone)
        if policy.verify_emails:
            email_ok = bool(
                contact.email
                and contact.verified_status in {VerificationStatus.VALID, VerificationStatus.CATCH_ALL}
            )
        else:
            email_ok = True
        if title_matches and has_contact_point and email_ok:
            return True

    return False


def _coerce_stage1_results(raw_results: Any) -> list[EnrichmentResult]:
    """Normalize None, a single EnrichmentResult, or any iterable/generator into a list."""
    if raw_results is None:
        return []
    if isinstance(raw_results, EnrichmentResult):
        return [raw_results]
    if isinstance(raw_results, (str, bytes, dict)):
        raise TypeError(f"Unsupported Stage 1 fetcher return type: {type(raw_results).__name__}")
    if isinstance(raw_results, Iterable):
        return [item for item in raw_results if item is not None]
    raise TypeError(f"Unsupported Stage 1 fetcher return type: {type(raw_results).__name__}")


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

        # --- Stage 1: First-party web, LinkedIn/public search, DNS/tech, registries ---
        for fetcher in self.stage1_fetchers:
            fetcher_input = current.model_copy(deep=True)
            try:
                raw_results = await fetcher(fetcher_input, active_policy)
                results = _coerce_stage1_results(raw_results)
            except Exception as exc:
                report.notes.append(f"stage1_fetcher_error:{type(exc).__name__}:{exc}")
                continue

            for res in results:
                explicit_stage = bool(res.stage and res.stage != "stage_1")
                explicit_credits = res.credits_used > 0.0
                effective_stage = res.stage if explicit_stage else "stage_1"
                effective_credits = res.credits_used if (explicit_stage or explicit_credits) else 0.0

                stage1_res = res.model_copy(
                    update={"stage": effective_stage, "credits_used": effective_credits}
                )
                current = merge_lead(current, stage1_res, active_policy, stage=effective_stage)
                report.lead = current
                report.stage_1_results += 1

        # --- Gate: Stop before spending API credits if Stage 1 already answered enough ---
        if not force_refresh and self.sufficiency_checker(current, active_policy):
            report.stopped_early_zero_credits = True
            report.lead = current
            return report

        # --- Stage 2: Cache lookup + ordered .env API fallback for remaining gaps ---
        if not active_policy.allow_stage_2_escalation or self.stage2_caller is None:
            if force_refresh and not active_policy.allow_stage_2_escalation:
                report.notes.append("force_refresh_blocked_by_policy_stage_2_disallowed")
            report.lead = current
            return report

        remaining_lead_budget = round(active_policy.max_credits_per_lead - current.credits_used, 6)
        bypass_lead_budget = bool(force_refresh)

        if remaining_lead_budget <= 0.0 and not bypass_lead_budget:
            report.notes.append("lead_credit_cap_reached_before_stage2")
            report.lead = current
            return report

        if bypass_lead_budget and remaining_lead_budget <= 0.0:
            report.notes.append("force_refresh_bypassed_lead_credit_cap")

        report.used_cache_or_api_fallback = True
        primary_contact = _select_stage2_contact(current, active_policy)

        async def _invoke_provider(provider: str, api_key: str) -> EnrichmentResult | None:
            assert self.stage2_caller is not None
            current_remaining = round(active_policy.max_credits_per_lead - current.credits_used, 6)
            if not bypass_lead_budget and current_remaining <= 0.0:
                report.notes.append(f"stage2_provider_skipped_budget:{provider}")
                return None

            res = await self.stage2_caller(provider, api_key, current, active_policy)
            if res is None:
                return None

            if not bypass_lead_budget and res.credits_used > current_remaining:
                report.notes.append(
                    f"stage2_result_exceeded_lead_budget:{provider}:cost={res.credits_used}:remaining={current_remaining}"
                )
                return None

            return res

        fallback_result, attempts = await self.cache.resolve_with_fallback(
            operation="waterfall_enrich",
            call_provider=_invoke_provider,
            estimated_credits_fn=(
                None
                if bypass_lead_budget
                else lambda _provider: remaining_lead_budget
            ),
            domain=current.domain,
            linkedin_url=primary_contact.linkedin_url,
            person_name=primary_contact.full_name,
            email=primary_contact.email,
            force_refresh=force_refresh,
        )
        report.fallback_attempts.extend(attempts)

        if fallback_result is not None:
            current_remaining = round(active_policy.max_credits_per_lead - current.credits_used, 6)
            if not bypass_lead_budget and fallback_result.credits_used > current_remaining:
                report.notes.append(
                    f"stage2_merge_blocked_by_lead_budget:cost={fallback_result.credits_used}:remaining={current_remaining}"
                )
            else:
                stage2_res = fallback_result.model_copy(update={"stage": "stage_2"})
                current = merge_lead(current, stage2_res, active_policy, stage="stage_2")

        report.lead = current
        return report

    async def enrich_batch(
        self,
        leads: Iterable[LeadRecord | dict[str, Any]],
        policy: EnrichmentPolicy | None = None,
        *,
        concurrency: int = 5,
        force_refresh: bool = False,
    ) -> list[WaterfallRunReport]:
        lead_items = list(leads)
        if not lead_items:
            return []

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _run_one(item: LeadRecord | dict[str, Any]) -> WaterfallRunReport:
            async with semaphore:
                try:
                    return await self.enrich_lead(item, policy=policy, force_refresh=force_refresh)
                except Exception as exc:
                    fallback_lead = (
                        item
                        if isinstance(item, LeadRecord)
                        else LeadRecord.model_validate(item.get("lead", {}) if isinstance(item, dict) and "lead" in item and isinstance(item["lead"], dict) else {})
                    )
                    return WaterfallRunReport(
                        lead=fallback_lead,
                        notes=[f"batch_lead_error:{type(exc).__name__}:{exc}"],
                    )

        return list(await asyncio.gather(*(_run_one(item) for item in lead_items)))


__all__ = [
    "EnrichmentWaterfall",
    "Stage1Fetcher",
    "Stage2ProviderCaller",
    "SufficiencyChecker",
    "WaterfallRunReport",
    "default_is_sufficient",
]