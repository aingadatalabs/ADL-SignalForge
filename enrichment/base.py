"""Shared schemas, policy, provider contract, and provenance-aware merge logic."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.contracts import BaseEnricher as CoreBaseEnricher


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.split("/", 1)[0].strip() or None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned if "@" in cleaned else None


def normalize_linkedin_url(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return cleaned or None


def _normalize_person_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _canonical_full_name(contact: ContactRecord) -> str:
    if contact.full_name and contact.full_name.strip():
        return _normalize_person_name(contact.full_name)
    parts = [p.strip() for p in (contact.first_name, contact.last_name) if p and p.strip()]
    return _normalize_person_name(" ".join(parts))


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VALID = "valid"
    CATCH_ALL = "catch_all"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class FieldProvenance(BaseModel):
    """Tracks where a specific field value came from and how trustworthy it is."""

    provider: str
    stage: str = "stage_1"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verified_status: VerificationStatus = VerificationStatus.UNVERIFIED
    enriched_at: datetime = Field(default_factory=utc_now)


class ContactRecord(BaseModel):
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    verified_status: VerificationStatus = VerificationStatus.UNVERIFIED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str | None = None

    @field_validator("email")
    @classmethod
    def _clean_email(cls, v: str | None) -> str | None:
        return normalize_email(v)

    @field_validator("linkedin_url")
    @classmethod
    def _clean_linkedin_url(cls, v: str | None) -> str | None:
        return normalize_linkedin_url(v)


class LeadRecord(BaseModel):
    """Canonical lead model shared across enrichment stages, cache, and qualification."""

    lead_id: str | None = None
    company_name: str | None = None
    domain: str | None = None
    industry: str | None = None
    company_size: str | None = None
    location: str | None = None
    website: str | None = None
    contacts: list[ContactRecord] = Field(default_factory=list)
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    extra_provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    credits_used: float = Field(default=0.0, ge=0.0)
    updated_at: datetime = Field(default_factory=utc_now)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, v: str | None) -> str | None:
        return normalize_domain(v)


class EnrichmentResult(BaseModel):
    """Standardized response returned by enricher methods."""

    source: str
    stage: str = "stage_1"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verified_status: VerificationStatus = VerificationStatus.UNVERIFIED
    credits_used: float = Field(default=0.0, ge=0.0)
    enriched_at: datetime = Field(default_factory=utc_now)
    company_patch: dict[str, Any] = Field(default_factory=dict)
    contacts: list[ContactRecord] = Field(default_factory=list)
    email_verified_status: VerificationStatus | None = None
    verified_email: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("verified_email")
    @classmethod
    def _clean_verified_email(cls, v: str | None) -> str | None:
        return normalize_email(v)


class EnrichmentPolicy(BaseModel):
    """Controls budget, quality thresholds, stage escalation, and caching behavior."""

    stage: Literal["stage_1", "stage_2", "waterfall"] = "waterfall"
    max_credits_per_lead: float = Field(default=10.0, ge=0.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    target_titles: list[str] = Field(default_factory=list)
    verify_emails: bool = True
    allow_stage_2_escalation: bool = True
    cache_ttl_seconds: int = Field(default=86_400, ge=0)
    overwrite_on_equal_confidence: bool = False


class BaseEnricher(CoreBaseEnricher, ABC):
    """
    Unified contract for enrichment providers across Stage 1 and Stage 2.

    Concrete providers may implement any subset of the enrichment entry points
    and should declare their capability flags (`supports_company_enrichment`,
    `supports_contact_discovery`, `supports_email_verification`,
    `supports_lead_signals`, `supports_raw_provider_call`).

    - Stage 1 signal fetchers (e.g., WebCrawlerProvider, DnsTechProvider,
      SearchLinkedInProvider, RegistriesProvider) can implement
      `fetch_lead_signals(lead, policy)` and optionally delegate or override
      `enrich_company(...)`.
    - Stage 2 B2B API adapters (e.g., Icypeas, Apollo, Hunter) can implement
      `enrich_company(...)`, `find_contacts(...)`, `verify_email(...)`, and/or
      `call_provider(action, payload, policy)`.
    """

    provider_name: str = "base"
    supports_company_enrichment: bool = True
    supports_contact_discovery: bool = True
    supports_email_verification: bool = True
    supports_lead_signals: bool = False
    supports_raw_provider_call: bool = False

    async def fetch_lead_signals(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy | None = None,
    ) -> EnrichmentResult:
        """Fetch lead-level signals (company patch + contacts) for a LeadRecord."""
        domain = normalize_domain(lead.domain) or normalize_domain(lead.website)
        if not domain:
            return EnrichmentResult(
                source=self.provider_name,
                stage="stage_1",
                confidence=0.0,
                verified_status=VerificationStatus.UNVERIFIED,
                credits_used=0.0,
                raw_payload={"reason": "missing_domain"},
            )
        active_policy = policy or EnrichmentPolicy()
        company_res = await self.enrich_company(domain, active_policy)
        contacts_res = await self.find_contacts(
            domain,
            target_titles=active_policy.target_titles or None,
            policy=active_policy,
        )
        merged_patch: dict[str, Any] = {}
        _deep_merge_dicts(merged_patch, company_res.company_patch)
        _deep_merge_dicts(merged_patch, contacts_res.company_patch)
        merged_contacts = _merge_contacts(company_res.contacts, contacts_res.contacts)
        best_conf = max(company_res.confidence, contacts_res.confidence)
        total_credits = round(company_res.credits_used + contacts_res.credits_used, 6)
        return EnrichmentResult(
            source=self.provider_name,
            stage=company_res.stage or contacts_res.stage or "stage_1",
            confidence=best_conf,
            verified_status=company_res.verified_status,
            credits_used=total_credits,
            company_patch=merged_patch,
            contacts=merged_contacts,
            raw_payload={
                "company": company_res.raw_payload,
                "contacts": contacts_res.raw_payload,
            },
        )

    async def call_provider(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        policy: EnrichmentPolicy | None = None,
    ) -> EnrichmentResult:
        """
        Generic action dispatcher for Stage 2 API adapters.

        Supported default actions:
        - "enrich_company": expects `domain` in `payload`
        - "find_contacts": expects `domain` (and optional `target_titles`) in `payload`
        - "verify_email": expects `email` in `payload`
        """
        data = payload or {}
        if action == "enrich_company":
            domain = normalize_domain(data.get("domain"))
            if not domain:
                raise ValueError("Missing 'domain' for action 'enrich_company'")
            return await self.enrich_company(domain, policy=policy)
        if action == "find_contacts":
            domain = normalize_domain(data.get("domain"))
            if not domain:
                raise ValueError("Missing 'domain' for action 'find_contacts'")
            titles = data.get("target_titles")
            return await self.find_contacts(domain, target_titles=titles, policy=policy)
        if action == "verify_email":
            email = normalize_email(data.get("email"))
            if not email:
                raise ValueError("Missing 'email' for action 'verify_email'")
            return await self.verify_email(email, policy=policy)
        raise NotImplementedError(
            f"Provider '{self.provider_name}' does not implement action '{action}'"
        )

    @abstractmethod
    async def enrich_company(
        self,
        domain: str,
        policy: EnrichmentPolicy | None = None,
    ) -> EnrichmentResult:
        """Enrich company metadata for a normalized domain."""

    @abstractmethod
    async def find_contacts(
        self,
        domain: str,
        target_titles: list[str] | None = None,
        policy: EnrichmentPolicy | None = None,
    ) -> EnrichmentResult:
        """Discover contacts matching target titles for a domain."""

    @abstractmethod
    async def verify_email(
        self,
        email: str,
        policy: EnrichmentPolicy | None = None,
    ) -> EnrichmentResult:
        """Verify deliverability/status of a single email address."""


EnrichmentProvider = BaseEnricher


def _should_replace_field(
    existing_val: Any,
    new_val: Any,
    existing_prov: FieldProvenance | None,
    new_prov: FieldProvenance,
    *,
    overwrite_on_equal_confidence: bool,
) -> bool:
    if new_val is None or (isinstance(new_val, str) and not new_val.strip()):
        return False
    if existing_val is None or (isinstance(existing_val, str) and not existing_val.strip()):
        return True
    if existing_prov is None:
        return True

    existing_verified = existing_prov.verified_status == VerificationStatus.VALID
    new_verified = new_prov.verified_status == VerificationStatus.VALID
    if new_verified and not existing_verified and new_prov.confidence >= existing_prov.confidence - 0.15:
        return True
    if existing_verified and not new_verified:
        return False

    if new_prov.confidence > existing_prov.confidence:
        return True
    if new_prov.confidence == existing_prov.confidence:
        return overwrite_on_equal_confidence
    return False


def _deep_merge_dicts(
    target: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge `incoming` dict into `target` dict without dropping sibling keys."""
    for key, new_val in incoming.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(new_val, dict)
        ):
            _deep_merge_dicts(target[key], new_val)
        else:
            target[key] = new_val.copy() if isinstance(new_val, dict) else new_val
    return target


def _find_matching_contact_index(
    contacts: list[ContactRecord],
    candidate: ContactRecord,
) -> int | None:
    """Match contacts across email, normalized LinkedIn URL, or canonical full name."""
    cand_email = (candidate.email or "").strip().lower()
    cand_li = normalize_linkedin_url(candidate.linkedin_url)
    cand_name = _canonical_full_name(candidate)

    for idx, existing in enumerate(contacts):
        ex_email = (existing.email or "").strip().lower()
        ex_li = normalize_linkedin_url(existing.linkedin_url)
        ex_name = _canonical_full_name(existing)

        if cand_email and ex_email and cand_email == ex_email:
            return idx
        if cand_li and ex_li and cand_li == ex_li:
            return idx
        if cand_name and ex_name and cand_name == ex_name:
            return idx

    return None


def _merge_single_contact(cur: ContactRecord, incoming: ContactRecord) -> ContactRecord:
    """Merge `incoming` into `cur` using pre-mutation baseline comparisons."""
    merged = cur.model_copy(deep=True)

    cur_conf = cur.confidence
    inc_conf = incoming.confidence
    cur_status = cur.verified_status
    inc_status = incoming.verified_status

    cur_is_valid = cur_status == VerificationStatus.VALID
    inc_is_valid = inc_status == VerificationStatus.VALID

    inc_email = normalize_email(incoming.email)
    cur_email = normalize_email(cur.email)
    if inc_email:
        if not cur_email:
            merged.email = inc_email
        elif inc_email != cur_email:
            if inc_is_valid and not cur_is_valid and inc_conf >= cur_conf - 0.15:
                merged.email = inc_email
            elif not cur_is_valid and inc_conf > cur_conf:
                merged.email = inc_email

    for field_name in ("full_name", "first_name", "last_name", "title", "phone", "linkedin_url"):
        raw_val = getattr(incoming, field_name)
        new_v = normalize_linkedin_url(raw_val) if field_name == "linkedin_url" else raw_val
        cur_v = getattr(cur, field_name)

        if new_v is None or (isinstance(new_v, str) and not new_v.strip()):
            continue
        if cur_v is None or (isinstance(cur_v, str) and not cur_v.strip()):
            setattr(merged, field_name, new_v)
        elif inc_conf > cur_conf and not (cur_is_valid and not inc_is_valid):
            setattr(merged, field_name, new_v)

    if inc_is_valid and (not cur_is_valid or inc_conf >= cur_conf):
        merged.verified_status = VerificationStatus.VALID
    elif cur_is_valid and not inc_is_valid:
        merged.verified_status = VerificationStatus.VALID
    elif cur_status == VerificationStatus.UNVERIFIED and inc_status != VerificationStatus.UNVERIFIED and inc_conf >= cur_conf:
        merged.verified_status = inc_status
    elif inc_conf > cur_conf and inc_status != VerificationStatus.UNVERIFIED:
        merged.verified_status = inc_status
    else:
        merged.verified_status = cur_status

    if inc_conf > cur_conf:
        merged.confidence = inc_conf
        merged.source = incoming.source or cur.source
    else:
        merged.confidence = cur_conf
        merged.source = cur.source or incoming.source

    return merged


def _merge_contacts(
    existing: list[ContactRecord],
    incoming: list[ContactRecord],
) -> list[ContactRecord]:
    merged_list: list[ContactRecord] = [c.model_copy(deep=True) for c in existing]

    for candidate in incoming:
        cleaned_candidate = candidate.model_copy(deep=True)
        match_idx = _find_matching_contact_index(merged_list, cleaned_candidate)
        if match_idx is None:
            merged_list.append(cleaned_candidate)
        else:
            merged_list[match_idx] = _merge_single_contact(merged_list[match_idx], cleaned_candidate)

    return merged_list


def _apply_targeted_email_verification(
    contacts: list[ContactRecord],
    verified_status: VerificationStatus,
    target_email: str | None,
) -> None:
    """Apply email verification status only to the specific verified contact email."""
    normalized_target = normalize_email(target_email)
    if normalized_target:
        for contact in contacts:
            if normalize_email(contact.email) == normalized_target:
                contact.verified_status = verified_status
        return

    # Fallback only when a single contact email exists on the lead and no target_email was specified
    emails_present = [c for c in contacts if normalize_email(c.email)]
    if len(emails_present) == 1:
        emails_present[0].verified_status = verified_status


_NON_PATCHABLE_FIELDS = frozenset(
    {"contacts", "provenance", "extra_provenance", "credits_used", "updated_at", "extra"}
)


def merge_lead(
    base: LeadRecord,
    result: EnrichmentResult,
    policy: EnrichmentPolicy | None = None,
    *,
    stage: str | None = None,
    verified_email: str | None = None,
) -> LeadRecord:
    """Merge an EnrichmentResult into a LeadRecord while preserving field-level provenance."""
    active_policy = policy or EnrichmentPolicy()
    effective_stage = stage or result.stage or active_policy.stage
    merged = base.model_copy(deep=True)

    prov = FieldProvenance(
        provider=result.source,
        stage=effective_stage,
        confidence=result.confidence,
        verified_status=result.verified_status,
        enriched_at=result.enriched_at,
    )

    patch = dict(result.company_patch)
    if "domain" in patch and patch["domain"]:
        patch["domain"] = normalize_domain(patch["domain"])

    extra_patch = patch.pop("extra", None)
    if isinstance(extra_patch, dict) and extra_patch:
        _deep_merge_dicts(merged.extra, extra_patch)
        for signal_key in extra_patch:
            merged.extra_provenance[signal_key] = prov.model_copy(deep=True)
        merged.provenance["extra"] = prov.model_copy(deep=True)

    for field_name, new_val in patch.items():
        if field_name in _NON_PATCHABLE_FIELDS:
            continue

        if field_name not in LeadRecord.model_fields:
            if isinstance(new_val, dict):
                _deep_merge_dicts(merged.extra, {field_name: new_val})
            else:
                merged.extra[field_name] = new_val
            merged.extra_provenance[field_name] = prov.model_copy(deep=True)
            continue

        existing_val = getattr(merged, field_name)
        existing_prov = merged.provenance.get(field_name)
        if _should_replace_field(
            existing_val,
            new_val,
            existing_prov,
            prov,
            overwrite_on_equal_confidence=active_policy.overwrite_on_equal_confidence,
        ):
            setattr(merged, field_name, new_val)
            merged.provenance[field_name] = prov.model_copy(deep=True)

    if result.contacts:
        merged.contacts = _merge_contacts(merged.contacts, result.contacts)
        merged.provenance["contacts"] = prov.model_copy(deep=True)

    if result.email_verified_status is not None:
        effective_target_email = verified_email or result.verified_email
        _apply_targeted_email_verification(
            merged.contacts,
            result.email_verified_status,
            effective_target_email,
        )

    merged.credits_used = round(merged.credits_used + result.credits_used, 6)
    merged.updated_at = utc_now()
    return merged


__all__ = [
    "BaseEnricher",
    "ContactRecord",
    "EnrichmentPolicy",
    "EnrichmentProvider",
    "EnrichmentResult",
    "FieldProvenance",
    "LeadRecord",
    "VerificationStatus",
    "merge_lead",
    "normalize_domain",
    "normalize_email",
    "normalize_linkedin_url",
]