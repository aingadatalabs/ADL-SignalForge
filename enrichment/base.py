"""Shared schemas, policy, provider contract, and provenance-aware merge logic."""

from __future__ import annotations

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
    raw_payload: dict[str, Any] = Field(default_factory=dict)


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
    """Standard contract for enrichment providers across Stage 1 and Stage 2."""

    provider_name: str = "base"

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

    # Verified status beats unverified/invalid when confidence is close
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


def _merge_contacts(
    existing: list[ContactRecord],
    incoming: list[ContactRecord],
) -> list[ContactRecord]:
    by_key: dict[str, ContactRecord] = {}
    order: list[str] = []

    def _key(c: ContactRecord) -> str:
        if c.email:
            return f"email:{c.email.lower()}"
        if c.linkedin_url:
            return f"li:{c.linkedin_url.lower()}"
        return f"name_title:{(c.full_name or '').lower()}|{(c.title or '').lower()}"

    for c in existing:
        k = _key(c)
        by_key[k] = c.model_copy(deep=True)
        order.append(k)

    for c in incoming:
        k = _key(c)
        if k not in by_key:
            by_key[k] = c.model_copy(deep=True)
            order.append(k)
            continue

        cur = by_key[k]
        for field_name in ("full_name", "first_name", "last_name", "title", "email", "phone", "linkedin_url"):
            new_v = getattr(c, field_name)
            cur_v = getattr(cur, field_name)
            if new_v and (not cur_v or c.confidence > cur.confidence):
                setattr(cur, field_name, new_v)

        if c.verified_status == VerificationStatus.VALID or (
            cur.verified_status == VerificationStatus.UNVERIFIED and c.confidence >= cur.confidence
        ):
            cur.verified_status = c.verified_status
        if c.confidence > cur.confidence:
            cur.confidence = c.confidence
            cur.source = c.source or cur.source

    return [by_key[k] for k in order]


def merge_lead(
    base: LeadRecord,
    result: EnrichmentResult,
    policy: EnrichmentPolicy | None = None,
    *,
    stage: str | None = None,
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

    for field_name, new_val in patch.items():
        if field_name not in LeadRecord.model_fields or field_name in {"contacts", "provenance", "credits_used", "updated_at"}:
            merged.extra[field_name] = new_val
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
        for contact in merged.contacts:
            if contact.email:
                contact.verified_status = result.email_verified_status

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
]