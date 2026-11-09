import logging
import re
from typing import Any, Literal
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("signalforge.qualification.rules")

RuleStatus = Literal["passed", "failed", "pending", "manual_review"]
DisqualificationReason = Literal[
    "blocklisted_domain",
    "excluded_entity_type",
    "negative_keyword_match",
    "out_of_bounds_geography",
    "invalid_team_size",
    "domain_health_failure",
    "pending_domain_resolution",
]

# Pre-compiled regex for performance optimization in batch processing
NUMBER_REGEX = re.compile(r"\d+")


class RuleCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    rule_id: str
    rule_name: str
    status: RuleStatus
    reason: str | None = None
    observed_value: Any | None = None
    expected_value: Any | None = None


class RuleEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    canonical_domain: str
    status: Literal["eligible", "disqualified", "pending", "manual_review"]
    checks: list[RuleCheck] = Field(default_factory=list)
    disqualification_reasons: list[str] = Field(default_factory=list)


class QualificationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    allowed_countries: list[str] = Field(
        default_factory=lambda: ["US", "CA", "GB", "AU", "KE", "ZA", "SG"]
    )
    blocked_domains: list[str] = Field(
        default_factory=lambda: [
            "clutch.co",
            "g2.com",
            "capterra.com",
            "upwork.com",
            "fiverr.com",
            "crunchbase.com",
        ]
    )
    blocked_keywords: list[str] = Field(
        default_factory=lambda: [
            "job board",
            "recruiting agency",
            "student blog",
            "personal portfolio",
        ]
    )
    min_team_size: int = 1
    max_team_size: int = 100


class CompanyQualificationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    company_id: str
    company_name: str
    canonical_domain: str
    website: str | None = None
    description: str | None = None
    services: list[str] = Field(default_factory=list)
    country_code: str | None = None
    team_size_text: str | None = None
    is_unresolved_candidate: bool = False


class DeterministicRuleEngine:
    def __init__(self, config: QualificationConfig | None = None):
        self.config = config or QualificationConfig()
        # Pre-process blocklists into sets for O(1) exact lookups
        self._exact_blocked_domains = {d.lower().strip(".") for d in self.config.blocked_domains}
        # Separate suffix/subdomain patterns if applicable
        self._suffix_blocked_domains = tuple(d.lower().strip(".") for d in self.config.blocked_domains)

    def evaluate(self, candidate_input: CompanyQualificationInput, verification: Any | None = None) -> RuleEvaluationResult:
        checks: list[RuleCheck] = []
        reasons: list[str] = []

        # R001: Domain Resolution Completeness (Pending Lifecycle Check)
        if candidate_input.is_unresolved_candidate or not candidate_input.canonical_domain:
            checks.append(RuleCheck(
                rule_id="R001",
                rule_name="Domain Resolution Completeness",
                status="pending",
                reason="Candidate lacks an explicit canonical domain and requires resolution.",
                observed_value=candidate_input.canonical_domain,
            ))
            return RuleEvaluationResult(
                company_id=candidate_input.company_id,
                canonical_domain=candidate_input.canonical_domain or "unresolved",
                status="pending",
                checks=checks,
                disqualification_reasons=["pending_domain_resolution"],
            )
        else:
            checks.append(RuleCheck(
                rule_id="R001",
                rule_name="Domain Resolution Completeness",
                status="passed",
                observed_value=candidate_input.canonical_domain,
            ))

        # R002: Blocklisted Domain Check (Bulletproof hostname parsing)
        hostname = self._extract_hostname(candidate_input.canonical_domain)
        domain_blocked = self._is_blocklisted(hostname)
        if domain_blocked:
            checks.append(RuleCheck(
                rule_id="R002",
                rule_name="Blocklisted Domain",
                status="failed",
                reason=f"Domain {hostname} matches known review portal or aggregator blocklist.",
                observed_value=hostname,
            ))
            reasons.append("blocklisted_domain")
        else:
            checks.append(RuleCheck(
                rule_id="R002",
                rule_name="Blocklisted Domain",
                status="passed",
                observed_value=hostname,
            ))

        # R003: Negative Keyword Check (Field-aware evaluation)
        text_corpus = f"{candidate_input.company_name} {candidate_input.description or ''} {' '.join(candidate_input.services)}".lower()
        matched_keyword = next((kw for kw in self.config.blocked_keywords if kw in text_corpus), None)
        if matched_keyword:
            checks.append(RuleCheck(
                rule_id="R003",
                rule_name="Excluded Business Model / Keyword",
                status="failed",
                reason=f"Matched excluded indicator: '{matched_keyword}'",
                observed_value=matched_keyword,
            ))
            reasons.append("negative_keyword_match")
        else:
            checks.append(RuleCheck(
                rule_id="R003",
                rule_name="Excluded Business Model / Keyword",
                status="passed",
            ))

        # R004: Geographic Hard Bounds Check
        if candidate_input.country_code and self.config.allowed_countries:
            norm_country = candidate_input.country_code.strip().upper()
            allowed = [c.upper() for c in self.config.allowed_countries]
            if norm_country not in allowed:
                checks.append(RuleCheck(
                    rule_id="R004",
                    rule_name="Campaign Geography",
                    status="failed",
                    reason=f"Country code '{norm_country}' is outside allowed target markets.",
                    observed_value=norm_country,
                    expected_value=allowed,
                ))
                reasons.append("out_of_bounds_geography")
            else:
                checks.append(RuleCheck(
                    rule_id="R004",
                    rule_name="Campaign Geography",
                    status="passed",
                    observed_value=norm_country,
                ))
        else:
            checks.append(RuleCheck(
                rule_id="R004",
                rule_name="Campaign Geography",
                status="manual_review",
                reason="Country information missing or unprovided; routed for manual review.",
            ))

        # R005: Team-Size Bounds Check (Interval Overlap with open-ended handling)
        if candidate_input.team_size_text:
            interval = self._parse_team_size_interval(candidate_input.team_size_text)
            if interval:
                min_sz, max_sz = interval
                # If max_sz is None (e.g. "50+"), check if min exceeds upper ceiling
                is_out_of_bounds = False
                if max_sz is not None:
                    if max_sz < self.config.min_team_size or min_sz > self.config.max_team_size:
                        is_out_of_bounds = True
                else:
                    if min_sz > self.config.max_team_size:
                        is_out_of_bounds = True

                if is_out_of_bounds:
                    checks.append(RuleCheck(
                        rule_id="R005",
                        rule_name="Team Size Bounds",
                        status="failed",
                        reason=f"Team size interval {interval} is entirely outside acceptable thresholds [{self.config.min_team_size}, {self.config.max_team_size}].",
                        observed_value=candidate_input.team_size_text,
                    ))
                    reasons.append("invalid_team_size")
                else:
                    checks.append(RuleCheck(
                        rule_id="R005",
                        rule_name="Team Size Bounds",
                        status="passed",
                        observed_value=candidate_input.team_size_text,
                    ))
            else:
                checks.append(RuleCheck(
                    rule_id="R005",
                    rule_name="Team Size Bounds",
                    status="manual_review",
                    reason=f"Could not reliably parse team size string: '{candidate_input.team_size_text}'",
                ))
        else:
            checks.append(RuleCheck(
                rule_id="R005",
                rule_name="Team Size Bounds",
                status="manual_review",
                reason="Team size data missing.",
            ))

        # Determine overall evaluation status
        if reasons:
            overall_status = "disqualified"
        elif any(c.status == "manual_review" for c in checks):
            overall_status = "manual_review"
        else:
            overall_status = "eligible"

        return RuleEvaluationResult(
            company_id=candidate_input.company_id,
            canonical_domain=candidate_input.canonical_domain,
            status=overall_status,
            checks=checks,
            disqualification_reasons=reasons,
        )

    def _extract_hostname(self, domain_str: str) -> str:
        """Safely extracts clean hostnames from raw domain strings, URLs, or ports using urlparse."""
        domain_str = domain_str.strip()
        if not domain_str:
            return ""
        
        # Ensure scheme exists for urlparse if omitted
        if "://" not in domain_str:
            domain_str = f"http://{domain_str}"
            
        parsed = urlparse(domain_str)
        hostname = parsed.hostname or parsed.path.split("/")[0]
        return hostname.lower().rstrip(".")

    def _is_blocklisted(self, hostname: str) -> bool:
        if not hostname:
            return False
        if hostname in self._exact_blocked_domains:
            return True
        return any(hostname.endswith("." + blocked) for blocked in self._suffix_blocked_domains)

    def _parse_team_size_interval(self, size_str: str) -> tuple[int, int | None] | None:
        numbers = NUMBER_REGEX.findall(size_str)
        if not numbers:
            return None
        parsed_nums = [int(n) for n in numbers]
        if len(parsed_nums) == 1:
            val = parsed_nums[0]
            if "+" in size_str:
                return (val, None) # Open-ended ceiling for 50+
            return (val, val)
        return (parsed_nums[0], parsed_nums[1])