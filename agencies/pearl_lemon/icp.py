"""Pearl Lemon's firmographic ICP, commercial signals, and evidence gates."""

import re
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agencies.adl.qualification_engine.rules import QualificationConfig

ICPStatus = Literal["eligible", "disqualified", "pending"]
OutreachReadiness = Literal["ready", "enrichment_required", "blocked"]


class ICPMatchAssessment(BaseModel):
    """Company-fit assessment, evidence gates, and separate outreach readiness."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: ICPStatus
    dimension_scores: dict[str, float]
    dimension_completeness: dict[str, float]
    composite_score: float
    team_size_status: Literal[
        "in_range", "potentially_in_range", "out_of_range", "unknown"
    ]
    evidence_gates: dict[str, bool]
    outreach_readiness: OutreachReadiness
    outreach_blockers: list[str] = Field(default_factory=list)
    matched_industries: list[str] = Field(default_factory=list)
    matched_job_titles: list[str] = Field(default_factory=list)
    matched_services: list[str] = Field(default_factory=list)
    matched_intent_signals: list[str] = Field(default_factory=list)
    matched_technologies: list[str] = Field(default_factory=list)
    disqualification_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scores(self) -> "ICPMatchAssessment":
        scores = [self.composite_score, *self.dimension_scores.values()]
        scores.extend(self.dimension_completeness.values())
        if any(not isfinite(score) or not 0 <= score <= 100 for score in scores):
            raise ValueError("ICP scores and completeness must be finite values from 0 to 100")
        if self.dimension_scores.keys() != self.dimension_completeness.keys():
            raise ValueError("Every score dimension must have a completeness value")
        return self


class PearlLemonICP(QualificationConfig):
    """Ideal Customer Profile for Pearl Lemon's B2B acquisition services.

    The weighted score ranks commercial fit. Evidence gates control qualification
    status independently, so a high score cannot bypass identity or verification.
    """

    allowed_countries: list[str] = Field(
        default_factory=lambda: [
            "GB", "US", "CA", "AU", "AT", "BE", "CH", "DE", "ES",
            "FR", "IE", "IT", "LI", "LU", "MC", "NL", "PT",
        ]
    )
    blocked_keywords: list[str] = Field(
        default_factory=lambda: [
            "job board",
            "recruiting agency",
            "student blog",
            "personal portfolio",
            "government",
            "non-profit",
            "nonprofit",
        ]
    )
    min_team_size: int = 10
    max_team_size: int = 250

    target_industries: set[str] = Field(
        default_factory=lambda: {
            "B2B SaaS",
            "E-commerce",
            "Professional Services",
            "Healthcare",
            "Financial Services",
        }
    )
    industry_aliases: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "B2B SaaS": [
                "b2b saas", "enterprise software", "software as a service",
                "workflow automation", "cybersecurity",
            ],
            "E-commerce": ["e-commerce", "e commerce", "ecommerce", "online retailer"],
            "Professional Services": [
                "professional services", "consulting", "recruitment",
                "business advisory", "specialist services", "law firm",
                "legal services", "commercial property", "real estate",
            ],
            "Healthcare": [
                "healthcare", "health tech", "healthtech", "medical services",
                "healthcare software", "dental",
            ],
            "Financial Services": [
                "financial services", "fintech", "financial technology",
                "financial software", "commercial finance", "wealth management",
                "private banking", "venture capital", "growth capital",
                "hedge fund", "corporate finance",
            ],
        }
    )
    target_geographies: set[str] = Field(
        default_factory=lambda: {
            "United Kingdom",
            "United States",
            "Canada",
            "Australia",
            "Western Europe",
        }
    )
    target_job_titles: set[str] = Field(
        default_factory=lambda: {
            "Chief Executive Officer",
            "Chief Marketing Officer",
            "VP of Marketing",
            "Head of Growth",
            "Digital Marketing Director",
            "Founder",
            "Co-Founder",
            "Managing Director",
            "Chief Revenue Officer",
            "VP of Sales",
            "CEO",
            "CMO",
            "VP Marketing",
            "VP Sales",
        }
    )
    service_alignment_signals: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "prospect research and list building": [
                "prospect research", "list building", "target account research",
            ],
            "cold email campaigns": ["cold email", "email outreach"],
            "LinkedIn prospecting": ["linkedin prospecting", "linkedin outreach"],
            "appointment setting": ["appointment setting", "booked meetings"],
            "outbound calling": ["outbound calling", "cold calling", "telemarketing"],
            "customer acquisition and pay-per-lead": [
                "customer acquisition", "pay per lead", "pay-per-lead",
                "lead generation outsourcing",
            ],
        }
    )
    buying_intent_signals: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "sales and business development recruitment": [
                "sales hiring", "sales recruitment", "business development hiring",
                "sales development representative",
                "sales development representatives",
                "hiring sales development representatives",
                "sdr hiring",
                "sales development rep",
            ],
            "new-market expansion": [
                "market expansion", "new market", "international expansion",
            ],
            "new product or service launch": [
                "product launch", "service launch", "new product",
            ],
            "new customer segments": [
                "new customer segment", "new target market", "new audience",
            ],
            "funding or expansion announcement": [
                "funding round", "raised funding", "expansion announcement",
            ],
            "new commercial leadership": [
                "new chief revenue officer", "new sales director",
                "appointed cmo", "appointed chief", "new head of sales",
            ],
            "growing sales operations": [
                "growing sales team", "sales operations", "scale sales",
                "sales team expansion",
            ],
        }
    )
    preferred_technologies: set[str] = Field(
        default_factory=lambda: {
            "HubSpot",
            "Salesforce",
            "Apollo",
            "Clay",
            "Lemlist",
            "Instantly",
            "Smartlead",
            "Shopify",
            "WordPress",
            "Google Analytics",
        }
    )
    excluded_industries: set[str] = Field(
        default_factory=lambda: {"Government", "Non-Profit"}
    )

    weight_firmographic_fit: float = Field(default=0.40, ge=0.0, le=1.0, allow_inf_nan=False)
    weight_service_alignment: float = Field(default=0.30, ge=0.0, le=1.0, allow_inf_nan=False)
    weight_buying_intent: float = Field(default=0.30, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_weights(self) -> "PearlLemonICP":
        total = (
            self.weight_firmographic_fit
            + self.weight_service_alignment
            + self.weight_buying_intent
        )
        if abs(total - 1.0) > 1e-5:
            raise ValueError(f"ICP weights must sum to 1.0, got {total}")
        return self

    def evaluate(self, account_data: Mapping[str, Any]) -> ICPMatchAssessment:
        """Assess raw or normalized account fields without treating missing data as a pass."""
        industry_values = self._values(
            account_data, "industry", "industries", "sector"
        )
        matched_industries = self._match_industries(industry_values)
        industry_known = bool(industry_values)

        geography_values = (
            self._values(account_data, "country_code")
            or self._values(account_data, "country", "geography", "location")
        )
        geography_match = self._matches_geography(geography_values)

        business_models = self._values(account_data, "business_model", "market_type")
        is_b2b = self._is_b2b(business_models)

        team_size_status = self._team_size_status(account_data)
        commercial_capacity = self._has_commercial_capacity(account_data)

        matched_job_titles = self._match_job_titles(account_data)
        matched_technologies = self._match_technologies(account_data)
        service_corpus = self._corpus(
            account_data,
            "acquisition_needs",
            "commercial_challenges",
            "growth_needs",
            "services_needed",
            "sales_challenges",
        )
        matched_services = self._matched_categories(
            service_corpus, self.service_alignment_signals
        )
        intent_corpus = self._corpus(
            account_data,
            "intent_signals",
            "recent_news",
            "job_openings",
            "announcements",
            "company_updates",
        )
        matched_intent_signals = self._matched_categories(
            intent_corpus, self.buying_intent_signals
        )

        team_size_factor = {
            "in_range": 1.0,
            "potentially_in_range": 0.5,
            "out_of_range": 0.0,
            "unknown": 0.0,
        }[team_size_status]
        firmographic_factors = (
            float(bool(matched_industries)),
            float(bool(geography_match)),
            float(is_b2b is True),
            team_size_factor,
            float(commercial_capacity is True),
        )
        firmographic_score = self._percentage(firmographic_factors)
        service_score = self._percentage(
            [
                self._category_matches(service_corpus, self.service_alignment_signals[category])
                for category in self.service_alignment_signals
            ]
        )
        intent_score = self._percentage(
            [
                self._category_matches(intent_corpus, self.buying_intent_signals[category])
                for category in self.buying_intent_signals
            ]
        )
        dimension_scores = {
            "firmographic_fit": firmographic_score,
            "service_alignment": service_score,
            "buying_intent": intent_score,
        }
        firmographic_completeness = self._percentage(
            (
                float(industry_known),
                float(bool(geography_values)),
                float(is_b2b is not None),
                float(team_size_status != "unknown"),
                float(commercial_capacity is not None),
            )
        )
        dimension_completeness = {
            "firmographic_fit": firmographic_completeness,
            "service_alignment": 100.0 if service_corpus else 0.0,
            "buying_intent": 100.0 if intent_corpus else 0.0,
        }
        composite_score = round(
            firmographic_score * self.weight_firmographic_fit
            + service_score * self.weight_service_alignment
            + intent_score * self.weight_buying_intent,
            2,
        )

        evidence_gates = self._evidence_gates(account_data)
        disqualification_reasons = self._disqualification_reasons(
            account_data,
            industry_values,
            matched_industries,
            geography_values,
            geography_match,
            business_models,
            is_b2b,
            team_size_status,
        )
        core_data_complete = (
            industry_known
            and bool(geography_values)
            and is_b2b is not None
            and team_size_status == "in_range"
        )

        if disqualification_reasons:
            status: ICPStatus = "disqualified"
        elif not core_data_complete or not all(
            evidence_gates[gate]
            for gate in (
                "company_identity_resolved",
                "website_evidence_available",
                "business_activity_supported",
            )
        ):
            status = "pending"
        else:
            status = "eligible"

        outreach_blockers = [
            gate for gate, passed in evidence_gates.items() if not passed
        ]
        if status == "pending":
            outreach_blockers.append("company_qualification_pending")
        elif status == "disqualified":
            outreach_blockers.append("company_disqualified")
        if status == "disqualified":
            outreach_readiness: OutreachReadiness = "blocked"
        elif outreach_blockers:
            outreach_readiness = "enrichment_required"
        else:
            outreach_readiness = "ready"

        return ICPMatchAssessment(
            status=status,
            dimension_scores=dimension_scores,
            dimension_completeness=dimension_completeness,
            composite_score=composite_score,
            team_size_status=team_size_status,
            evidence_gates=evidence_gates,
            outreach_readiness=outreach_readiness,
            outreach_blockers=outreach_blockers,
            matched_industries=matched_industries,
            matched_job_titles=matched_job_titles,
            matched_services=matched_services,
            matched_intent_signals=matched_intent_signals,
            matched_technologies=matched_technologies,
            disqualification_reasons=disqualification_reasons,
        )

    def is_match(self, account_data: Mapping[str, Any]) -> bool:
        """Return whether the company meets ICP criteria, not outreach readiness."""
        return self.evaluate(account_data).status == "eligible"

    def is_valid_domain(self, value: Any) -> bool:
        """Public validation contract for canonical domains used by enrichment."""
        return self._valid_domain(value)

    def match_industries(self, values: Sequence[str]) -> list[str]:
        """Public industry matcher for enrichment and later qualification stages."""
        return self._match_industries(list(values))

    def matches_geography(self, values: Sequence[str]) -> bool | None:
        """Public geography matcher for enrichment and downstream validation."""
        return self._matches_geography(list(values))

    def _match_industries(self, values: list[str]) -> list[str]:
        matches: list[str] = []
        normalized_values = [self._normalize(value) for value in values if value.strip()]
        for industry in self.target_industries:
            terms = self.industry_aliases.get(industry, []) + [industry]
            if any(
                self._contains_phrase(value, self._normalize(term))
                for value in normalized_values
                for term in terms
            ):
                matches.append(industry)
        return sorted(matches)

    def _matches_geography(self, values: list[str]) -> bool | None:
        if not values:
            return None
        country_aliases = {
            "united kingdom": "GB", "great britain": "GB", "uk": "GB",
            "england": "GB", "scotland": "GB", "wales": "GB",
            "northern ireland": "GB",
            "united states": "US", "united states of america": "US", "usa": "US",
            "canada": "CA", "australia": "AU",
            "austria": "AT", "belgium": "BE", "switzerland": "CH",
            "germany": "DE", "spain": "ES", "france": "FR", "ireland": "IE",
            "italy": "IT", "liechtenstein": "LI", "luxembourg": "LU",
            "monaco": "MC", "netherlands": "NL", "the netherlands": "NL",
            "portugal": "PT",
        }
        allowed = {self._normalize(code).upper() for code in self.allowed_countries}
        western_europe = {"AT", "BE", "CH", "DE", "ES", "FR", "IE", "IT", "LI", "LU", "MC", "NL", "PT"}
        for value in values:
            normalized = self._normalize(value)
            if normalized in {"western europe", "western european"}:
                if allowed & western_europe:
                    return True
                continue
            for name, code in country_aliases.items():
                if self._contains_phrase(normalized, name) and code in allowed:
                    return True
            for code in allowed:
                if re.search(rf"(?<![a-z]){re.escape(code.lower())}(?![a-z])", normalized):
                    return True
        return False

    @staticmethod
    def _is_b2b(values: list[str]) -> bool | None:
        if not values:
            return None
        text = " ".join(" ".join(values).casefold().split())
        if re.search(r"\bnot\s+(?:exclusively\s+)?b2b\b", text):
            return None
        has_b2b = bool(re.search(r"\bb2b\b|\bbusiness to business\b", text))
        has_b2c = bool(
            re.search(r"\bb2c\b|\bbusiness to consumer\b|\bconsumer-only\b", text)
        )
        if has_b2b:
            return True
        if has_b2c:
            return False
        return None

    def _team_size_status(
        self, data: Mapping[str, Any]
    ) -> Literal["in_range", "potentially_in_range", "out_of_range", "unknown"]:
        raw_count = data.get("employee_count")
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
            if not isfinite(raw_count) or not float(raw_count).is_integer() or raw_count < 0:
                return "unknown"
            count = int(raw_count)
            return (
                "in_range"
                if self.min_team_size <= count <= self.max_team_size
                else "out_of_range"
            )
        if isinstance(raw_count, str) and raw_count.strip().isdigit():
            count = int(raw_count.strip())
            return (
                "in_range"
                if self.min_team_size <= count <= self.max_team_size
                else "out_of_range"
            )
        values = self._values(
            data, "employee_count", "team_size_text", "employee_range", "team_size"
        )
        if not values:
            return "unknown"
        value = values[0].replace(",", "")
        numbers = [int(number) for number in re.findall(r"\d+", value)]
        if not numbers:
            return "unknown"
        lower = numbers[0]
        is_open_ended = "+" in value or bool(
            re.search(r"\b(?:at least|over|more than)\b", value, re.IGNORECASE)
        )
        upper = numbers[1] if len(numbers) > 1 and not is_open_ended else (
            None if is_open_ended else lower
        )
        if upper is not None and upper < self.min_team_size:
            return "out_of_range"
        if lower > self.max_team_size:
            return "out_of_range"
        if lower >= self.min_team_size and upper is not None and upper <= self.max_team_size:
            return "in_range"
        if upper is None and lower >= self.min_team_size and lower > self.max_team_size:
            return "out_of_range"
        if lower == upper and self.min_team_size <= lower <= self.max_team_size:
            return "in_range"
        return "potentially_in_range"

    def _match_job_titles(self, data: Mapping[str, Any]) -> list[str]:
        titles: list[str] = []
        profile = data.get("enrichment_profile")
        decision_makers = data.get("decision_makers") or self._field_value(
            profile, "decision_makers"
        )
        if isinstance(decision_makers, Sequence) and not isinstance(
            decision_makers, (str, bytes)
        ):
            for person in decision_makers:
                if isinstance(person, Mapping):
                    titles.extend(self._values(person, "title", "job_title"))
                else:
                    titles.extend(
                        value
                        for value in (
                            getattr(person, "title", None),
                            getattr(person, "job_title", None),
                        )
                        if isinstance(value, str)
                    )
        titles.extend(self._values(data, "decision_maker_title", "job_title"))
        return sorted(
            {
                title
                for title in titles
                if self._is_target_job_title(title)
            }
        )

    def _is_target_job_title(self, title: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        if not normalized or re.search(
            r"\b(?:assistant|associate|intern|coordinator|administrator)\b",
            normalized,
        ):
            return False
        return any(
            self._contains_phrase(normalized, re.sub(r"[^a-z0-9]+", " ", target.casefold()).strip())
            for target in self.target_job_titles
        )

    def _match_technologies(self, data: Mapping[str, Any]) -> list[str]:
        values = self._values(
            data,
            "technologies",
            "tech_stack",
            "technology_stack",
            "technographic_signals",
        )
        for key in (
            "technologies",
            "tech_stack",
            "technology_stack",
            "technographic_signals",
        ):
            items = data.get(key) or self._field_value(
                data.get("enrichment_profile"), key
            )
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                for item in items:
                    if isinstance(item, Mapping):
                        values.extend(self._values(item, "name", "technology"))
                    else:
                        values.extend(
                            name
                            for name in (
                                getattr(item, "name", None),
                                getattr(item, "technology", None),
                            )
                            if isinstance(name, str)
                        )
        normalized_values = [self._normalize(value) for value in values]
        return sorted(
            technology
            for technology in self.preferred_technologies
            if any(
                self._contains_phrase(value, self._normalize(technology))
                for value in normalized_values
            )
        )

    @staticmethod
    def _has_commercial_capacity(data: Mapping[str, Any]) -> bool | None:
        keys = (
            "has_internal_sales_team",
            "proven_offer",
            "commercial_capacity",
            "annual_turnover_gbp_5m_plus",
        )
        observed = [data[key] for key in keys if isinstance(data.get(key), bool)]
        return any(observed) if observed else None

    @classmethod
    def _evidence_gates(cls, data: Mapping[str, Any]) -> dict[str, bool]:
        identity = data.get("company_id") or data.get("company_name")
        domain = data.get("canonical_domain") or data.get("domain")
        identity_resolved = data.get("company_identity_resolved") is True or (
            bool(identity)
            and cls._valid_domain(domain)
            and str(identity).strip().casefold() not in {"unknown", "unresolved", "none"}
        )
        website_evidence = cls._has_website_evidence(data)
        activity_supported = cls._has_activity_evidence(data)
        decision_maker_identified = cls._has_decision_maker_evidence(data)
        contact_channel_verified = (
            data.get("contact_channel_verified") is True
            or data.get("verified_contact_channel") is True
            or data.get("contact_channel_verification") == "verified"
            or cls._has_verified_contact(data.get("contact_channels"))
            or cls._has_verified_email(data)
        )
        return {
            "company_identity_resolved": identity_resolved,
            "website_evidence_available": website_evidence,
            "business_activity_supported": activity_supported,
            "decision_maker_identified": decision_maker_identified,
            "contact_channel_verified_before_outreach": contact_channel_verified,
        }

    @classmethod
    def _has_verified_contact(cls, value: Any) -> bool:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return any(
                cls._field_value(contact, "verified") is True
                and bool(
                    cls._field_value(contact, "value")
                    or cls._field_value(contact, "email")
                    or cls._field_value(contact, "phone")
                )
                for contact in value
            )
        return False

    @classmethod
    def _has_website_evidence(cls, data: Mapping[str, Any]) -> bool:
        if data.get("website_evidence_available") is True or (
            cls._valid_url(data.get("website"))
            and (
                cls._valid_url(data.get("website_source_url"))
                or cls._valid_url(data.get("website_evidence"))
                or cls._valid_url(data.get("website_evidence_url"))
            )
        ):
            return True
        crawl = data.get("crawl_result")
        pages = cls._field_value(crawl, "pages")
        return isinstance(pages, Sequence) and any(
            cls._field_value(page, "fetch_status") == "success"
            and bool(str(cls._field_value(page, "cleaned_text") or "").strip())
            for page in pages
        )

    @classmethod
    def _has_verified_email(cls, data: Mapping[str, Any]) -> bool:
        report = data.get("verification_report")
        company_id = str(data.get("company_id") or "")
        domain = cls._normalized_domain(data.get("canonical_domain") or data.get("domain"))
        report_company_id = str(cls._field_value(report, "company_id") or "")
        report_domain = cls._normalized_domain(
            cls._field_value(report, "canonical_domain")
        )
        if (
            not company_id
            or not domain
            or company_id != report_company_id
            or domain != report_domain
        ):
            return False
        candidates = cls._field_value(report, "email_candidates")
        if not isinstance(candidates, Sequence):
            return False
        decision_makers = data.get("decision_makers") or cls._field_value(
            data.get("enrichment_profile"), "decision_makers"
        )
        people = list(decision_makers) if isinstance(decision_makers, Sequence) and not isinstance(decision_makers, (str, bytes)) else []
        for candidate in candidates:
            email = str(cls._field_value(candidate, "email") or "").strip()
            local_part, separator, raw_email_domain = email.partition("@")
            email_domain = raw_email_domain.casefold()
            if not (
                separator
                and local_part
                and not any(character.isspace() for character in email)
                and ".." not in local_part
                and not local_part.startswith(".")
                and not local_part.endswith(".")
                and (email_domain == domain or email_domain.endswith("." + domain))
                and cls._valid_domain(email_domain)
                and cls._field_value(candidate, "evidence_level") == "observed"
                and cls._field_value(candidate, "syntax_valid") is True
                and cls._field_value(candidate, "domain_mail_status")
                in {"mx_present", "implicit_mx"}
            ):
                continue
            candidate_person_id = cls._field_value(candidate, "person_id")
            candidate_name = cls._normalize(
                str(cls._field_value(candidate, "full_name") or "")
            )
            if any(
                (
                    candidate_person_id
                    and candidate_person_id == cls._field_value(person, "person_id")
                )
                or (
                    candidate_name
                    and candidate_name
                    == cls._normalize(str(cls._field_value(person, "full_name") or ""))
                )
                for person in people
            ):
                return True
        return False

    @classmethod
    def _has_activity_evidence(cls, data: Mapping[str, Any]) -> bool:
        if data.get("business_activity_supported") is True:
            return True
        evidence = data.get("business_activity_evidence")
        if isinstance(evidence, Mapping):
            claim = str(evidence.get("claim") or "").strip()
            return (
                len(claim) >= 12
                and cls._normalize(claim) not in {"unverified", "unknown", "none", "pending"}
                and cls._valid_url(evidence.get("source_url"))
                and evidence.get("verification_status") == "verified"
            )
        if not isinstance(evidence, str):
            return False
        claim = evidence.strip()
        return (
            len(claim) >= 12
            and cls._normalize(claim) not in {"unverified", "unknown", "none", "pending"}
            and cls._valid_url(data.get("business_activity_source_url"))
        )

    @classmethod
    def _has_decision_maker_evidence(cls, data: Mapping[str, Any]) -> bool:
        if data.get("decision_maker_identified") is True:
            return True
        people = data.get("decision_makers") or cls._field_value(
            data.get("enrichment_profile"), "decision_makers"
        )
        if not isinstance(people, Sequence) or isinstance(people, (str, bytes)):
            return False
        return any(
            bool(str(cls._field_value(person, "full_name") or "").strip())
            and bool(
                str(
                    cls._field_value(person, "title")
                    or cls._field_value(person, "job_title")
                    or ""
                ).strip()
            )
            and (
                cls._valid_url(cls._field_value(person, "source_url"))
            )
            for person in people
        )

    @classmethod
    def _valid_domain(cls, value: Any) -> bool:
        domain = cls._normalized_domain(value)
        if not domain or domain in {"unknown", "unresolved", "none"}:
            return False
        labels = domain.rstrip(".").split(".")
        return len(labels) >= 2 and all(
            label
            and len(label) <= 63
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        )

    @classmethod
    def _normalized_domain(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        raw = value.strip()
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        return (parsed.hostname or "").rstrip(".").casefold()

    @classmethod
    def _valid_url(cls, value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        parsed = urlsplit(value.strip())
        return (
            parsed.scheme in {"http", "https"}
            and cls._valid_domain(parsed.hostname)
        )

    @staticmethod
    def _field_value(value: Any, name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    def _disqualification_reasons(
        self,
        account_data: Mapping[str, Any],
        industry_values: list[str],
        matched_industries: list[str],
        geography_values: list[str],
        geography_match: bool | None,
        business_models: list[str],
        is_b2b: bool | None,
        team_size_status: str,
    ) -> list[str]:
        reasons: list[str] = []
        if any(
            self._is_excluded_industry(value)
            for value in industry_values
        ):
            reasons.append("excluded_industry")
        elif industry_values and not matched_industries:
            reasons.append("industry_out_of_target")
        for keyword in self._blocked_keyword_matches(account_data):
            reason_keyword = re.sub(r"[^a-z0-9]+", "_", self._normalize(keyword)).strip("_")
            reasons.append(f"blocked_keyword:{reason_keyword}")
        if geography_values and geography_match is False:
            reasons.append("geography_out_of_target")
        if business_models and is_b2b is False:
            reasons.append("non_b2b_business_model")
        if team_size_status == "out_of_range":
            reasons.append("employee_count_out_of_range")
        return reasons

    @staticmethod
    def _percentage(factors: Sequence[float | bool]) -> float:
        return round(sum(factors) / len(factors) * 100, 2) if factors else 0.0

    @classmethod
    def _matched_categories(
        cls, corpus: str, categories: Mapping[str, list[str]]
    ) -> list[str]:
        return [
            category
            for category, terms in categories.items()
            if cls._category_matches(corpus, terms)
        ]

    @classmethod
    def _category_matches(cls, corpus: str, terms: Sequence[str]) -> bool:
        for term in terms:
            for match in re.finditer(
                rf"(?<![a-z0-9]){re.escape(cls._normalize(term))}(?![a-z0-9])",
                corpus,
            ):
                preceding_text = corpus[max(0, match.start() - 40):match.start()]
                if not re.search(
                    r"\b(?:not|no|never|without|avoid|avoids|stopped|"
                    r"don't|doesn't|do not|does not)\s+(?:\w+\s+){0,2}$",
                    preceding_text,
                ):
                    return True
        return False

    def _blocked_keyword_matches(self, data: Mapping[str, Any]) -> list[str]:
        fields = (
            "company_name",
            "industry",
            "industries",
            "sector",
            "description",
            "business_description",
            "website_title",
            "website_content",
            "business_model",
            "company_type",
            "organization_type",
        )
        structured_fields = (
            "company_name",
            "industry",
            "industries",
            "sector",
            "business_model",
            "company_type",
            "organization_type",
        )
        values = self._values(data, *fields)
        structured_values = self._values(data, *structured_fields)
        matches = []
        for keyword in self.blocked_keywords:
            normalized_keyword = self._normalize(keyword)
            if normalized_keyword in {
                "government",
                "non profit",
                "nonprofit",
            }:
                matched = any(
                    self._contains_phrase(value, normalized_keyword)
                    for value in structured_values
                )
            elif normalized_keyword == "recruiting agency":
                matched = any(
                    self._contains_phrase(value, normalized_keyword)
                    and not (
                        self._contains_phrase(value, "software")
                        or self._contains_phrase(value, "platform")
                    )
                    for value in structured_values
                )
            else:
                matched = any(
                    self._contains_phrase(value, normalized_keyword)
                    for value in values
                )
            if matched:
                matches.append(keyword)
        return matches

    @classmethod
    def _matches_any(cls, value: str, candidates: Sequence[str] | set[str]) -> bool:
        return any(
            cls._contains_phrase(cls._normalize(value), cls._normalize(candidate))
            for candidate in candidates
            if cls._normalize(candidate)
        )

    def _is_excluded_industry(self, value: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", self._normalize(value)).strip()
        if normalized in {"nonprofit", "non profit"}:
            return True
        return self._matches_any(value, self.excluded_industries)

    @staticmethod
    def _contains_phrase(text: str, phrase: str) -> bool:
        if not phrase:
            return False
        text = " ".join(text.casefold().split())
        phrase = " ".join(phrase.casefold().split())
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
                text,
            )
        )

    @classmethod
    def _corpus(cls, data: Mapping[str, Any], *keys: str) -> str:
        return " ".join(
            cls._normalize(value)
            for key in keys
            for value in cls._values(data, key)
        )

    @staticmethod
    def _values(data: Mapping[str, Any], *keys: str) -> list[str]:
        values: list[str] = []
        for key in keys:
            value = data.get(key)
            if isinstance(value, str):
                if value.strip():
                    values.append(value.strip())
            elif isinstance(value, Mapping):
                for nested_key in ("name", "value", "label", "code", "title"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str) and nested_value.strip():
                        values.append(nested_value.strip())
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                values.extend(
                    item.strip()
                    for item in value
                    if isinstance(item, str) and item.strip()
                )
            elif isinstance(value, set):
                values.extend(
                    item.strip()
                    for item in value
                    if isinstance(item, str) and item.strip()
                )
        return values

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())


DEFAULT_PEARL_LEMON_ICP = PearlLemonICP()

__all__ = [
    "DEFAULT_PEARL_LEMON_ICP",
    "ICPMatchAssessment",
    "OutreachReadiness",
    "PearlLemonICP",
]
