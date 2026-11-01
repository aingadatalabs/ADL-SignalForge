from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

FetchStatus = Literal[
    "success",
    "not_found",
    "forbidden",
    "rate_limited",
    "timeout",
    "connection_error",
    "invalid_content",
    "blocked",
    "redirect_rejected",
    "invalid_domain",
]


class NormalizedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    company_name: str | None = None
    canonical_domain: str
    website: str | None = None
    lead_score: int | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class CrawledPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    url: str
    path: str
    discovery_method: Literal["known_path", "internal_link"] = "known_path"
    fetch_status: FetchStatus
    status_code: int | None = None
    content_type: str | None = None
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    content_hash: str | None = None
    raw_html_ref: str | None = None
    cleaned_text: str | None = None
    error: str | None = None
    internal_links: list[str] = Field(default_factory=list)


class CrawlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    crawl_metadata: dict[str, Any] = Field(default_factory=dict)
    pages: list[CrawledPage] = Field(default_factory=list)


EmailType = Literal[
    "named_corporate",
    "generic_corporate",
    "departmental",
    "personal_provider",
    "unknown",
]


class ContactEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    value: str
    email_type: EmailType = "unknown"
    source_url: str
    source_field: Literal["mailto", "text", "meta", "json_ld"] = "text"
    confidence: float = 1.0
    associated_person_name: str | None = Field(
        default=None, description="Name of person explicitly associated with this email, if any"
    )
    evidence: str | None = Field(
        default=None, description="Text excerpt supporting this contact extraction"
    )


class SocialProfileEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    platform: str = "linkedin"
    profile_url: str
    source_url: str
    confidence: float = 1.0


class TechnologyEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    name: str
    category: str
    source_url: str
    evidence: str
    confidence: float = 0.95


class DecisionMaker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    person_id: str | None = Field(default=None, description="Stable identifier for entity resolution")
    full_name: str = Field(description="Full name of the decision maker")
    title: str = Field(description="Job title, e.g., Founder, CEO, CTO, Head of Growth")
    linkedin_url: str | None = Field(default=None, description="Direct LinkedIn profile URL if found")
    corporate_email: str | None = Field(default=None, description="Corporate email explicitly observed and attributed to this person")
    source_url: str | None = Field(default=None, description="Page URL where this person was identified")
    evidence: str | None = Field(default=None, description="Text excerpt supporting this extraction")
    confidence: float = Field(default=0.9, description="Extraction confidence score")


class AgencyEnrichmentProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    decision_makers: list[DecisionMaker] = Field(default_factory=list)
    contacts: list[ContactEvidence] = Field(default_factory=list)
    social_profiles: list[SocialProfileEvidence] = Field(default_factory=list)
    tech_stack: list[TechnologyEvidence] = Field(default_factory=list)
    estimated_team_size: str | None = Field(default=None, description="Approximate employee count range")
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


# --- Verification Schemas ---

EmailEvidenceLevel = Literal[
    "observed",
    "pattern_inferred",
    "domain_valid_generated",
    "unverified",
]

PersonAssociationStatus = Literal[
    "directly_observed",
    "pattern_supported",
    "unconfirmed",
    "not_applicable",
]

DomainMailStatus = Literal[
    "mx_present",
    "implicit_mx",
    "null_mx",
    "nxdomain",
    "dns_no_answer",
    "dns_timeout",
    "dns_error",
    "invalid_domain",
]


class EmailCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    person_id: str | None = Field(default=None, description="Stable identifier linking to DecisionMaker")
    full_name: str
    email: str
    permutation_type: str = Field(description="e.g., first.last, first, flast")
    syntax_valid: bool
    domain_mail_status: DomainMailStatus
    evidence_level: EmailEvidenceLevel = "unverified"
    person_association_status: PersonAssociationStatus = "unconfirmed"
    source_reference: str | None = Field(default=None, description="Origin URL or pattern context")
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    email_candidates: list[EmailCandidate] = Field(default_factory=list)
    domain_mx_hosts: list[str] = Field(default_factory=list)
    verification_metadata: dict[str, Any] = Field(default_factory=dict)