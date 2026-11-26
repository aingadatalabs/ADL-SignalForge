"""Pearl Lemon's firmographic ICP, commercial signals, and evidence gates."""

import re
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agencies.adl.qualification_engine.rules import QualificationConfig

ICPStatus = Literal["eligible", "disqualified", "pending"]
OutreachReadiness = Literal["ready", "enrichment_required", "blocked"]
BusinessModel = Literal["b2b", "b2c", "b2b_b2c", "unknown"]
FirmographicFactStatus = Literal[
    "matched",
    "out_of_target",
    "confirmed",
    "not_confirmed",
    "in_range",
    "potentially_in_range",
    "out_of_range",
    "unknown",
]


class FirmographicFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    status: FirmographicFactStatus
    observed_value: Any | None = None


class ICPMatchAssessment(BaseModel):
    """Company-fit assessment, evidence gates, and separate outreach readiness."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: ICPStatus
    dimension_scores: dict[str, float]
    dimension_completeness: dict[str, float]
    composite_score: float
    business_model: BusinessModel = "unknown"
    team_size_status: Literal[
        "in_range", "potentially_in_range", "out_of_range", "unknown"
    ]
    evidence_gates: dict[str, bool]
    outreach_readiness: OutreachReadiness
    firmographic_facts: dict[str, FirmographicFact] = Field(default_factory=dict)
    outreach_blockers: list[str] = Field(default_factory=list)
    verified_decision_maker_emails: list[str] = Field(default_factory=list)
    matched_industries: list[str] = Field(default_factory=list)
    matched_job_titles: list[str] = Field(default_factory=list)
    matched_services: list[str] = Field(default_factory=list)
    matched_acquisition_pains: list[str] = Field(default_factory=list)
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
    max_team_size: int | None = 250

    target_industries: set[str] = Field(
        default_factory=lambda: {
            "B2B SaaS",
            "E-commerce",
            "Professional Services",
            "Healthcare",
            "Financial Services",
            "Technology",
            "Manufacturing",
            "Property and Real Estate",
        }
    )
    industry_aliases: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "B2B SaaS": [
                "b2b saas", "enterprise software", "software as a service",
            ],
            "E-commerce": ["e-commerce", "e commerce", "ecommerce", "online retailer"],
            "Professional Services": [
                "professional services", "consulting",
                "business advisory", "specialist services", "law firm",
                "legal services",
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
            "Technology": [
                "technology", "information technology", "it services",
            ],
            "Manufacturing": [
                "manufacturing", "manufacturer", "industrial equipment",
                "industrial products",
            ],
            "Property and Real Estate": [
                "property", "real estate", "commercial property",
                "property development", "real estate investment",
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
            "Marketing Director",
            "Director of Marketing",
            "Sales Director",
            "Director of Sales",
            "Growth Director",
            "Director of Growth",
            "Revenue Director",
            "Director of Revenue",
            "Business Development Director",
            "Director of Business Development",
            "Commercial Director",
            "Director of Commercial",
            "Sales Operations Director",
            "Director of Sales Operations",
            "Founder",
            "Co-Founder",
            "Managing Director",
            "Chief Revenue Officer",
            "VP of Sales",
            "President",
            "Owner",
            "Principal",
            "Partner",
            "Head of Marketing",
            "Head of Sales",
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
    acquisition_pain_signals: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "poor target-account fit": [
                "wrong prospects",
                "wrong target accounts",
                "poor targeting",
                "targeting the wrong",
                "low-quality leads",
                "low quality leads",
                "irrelevant leads",
            ],
            "underperforming or outdated outreach": [
                "outdated outreach",
                "underperforming outreach",
                "generic outreach",
                "low response rates",
                "poor response rates",
                "single-channel outreach",
            ],
            "inconsistent lead qualification": [
                "lack of repeatable qualification",
                "lack a repeatable qualification",
                "lacks repeatable qualification",
                "no repeatable qualification",
                "inconsistent lead qualification",
                "poor lead qualification",
                "unqualified leads",
            ],
            "unpredictable sales pipeline": [
                "unpredictable pipeline",
                "pipeline is unpredictable",
                "inconsistent pipeline",
                "pipeline gaps",
                "weak pipeline",
                "lack of pipeline visibility",
                "not enough qualified opportunities",
            ],
            "manual prospect research": [
                "manual prospect research",
                "manually researching prospects",
                "manual lead research",
                "manual list building",
                "spreadsheet-based prospecting",
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
        industry_values = self._values(account_data, "primary_industry")
        if not industry_values and not account_data.get("industry_evidence"):
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
        business_model_evidence = account_data.get("business_model_evidence")
        if (
            business_model_evidence
            and not self.is_verified_business_model_evidence(
                business_model_evidence
            )
        ):
            business_models = []
        customer_values = self._values(
            account_data,
            "customer_types",
            "customer_segments",
            "target_customers",
            "target_market",
            "customer_base",
        )
        business_model = self._classify_business_model(
            business_models, customer_values
        )
        has_b2b_customers = business_model in {"b2b", "b2b_b2c"}

        team_size_status = self._team_size_status(account_data)
        commercial_maturity = self._has_commercial_maturity(account_data)
        sales_infrastructure = self._has_sales_infrastructure(account_data)
        acquisition_capacity = self._has_acquisition_capacity(account_data)
        existing_sales_pipeline = self._has_existing_sales_pipeline(account_data)

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
        acquisition_pain_corpus = self._corpus(
            account_data,
            "pain_points",
            "acquisition_challenges",
            "lead_generation_challenges",
            "commercial_challenges",
            "sales_challenges",
            "growth_needs",
            "acquisition_needs",
        )
        matched_acquisition_pains = self._matched_categories(
            acquisition_pain_corpus, self.acquisition_pain_signals
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
        commercial_facts = (
            commercial_maturity,
            sales_infrastructure,
            acquisition_capacity,
            existing_sales_pipeline,
        )
        commercial_readiness_score = self._percentage(
            [fact is True for fact in commercial_facts]
        )
        commercial_completeness = self._percentage(
            [fact is not None for fact in commercial_facts]
        )
        firmographic_factors = (
            float(bool(matched_industries)),
            float(bool(geography_match)),
            float(has_b2b_customers),
            team_size_factor,
            commercial_readiness_score / 100,
        )
        firmographic_score = self._percentage(firmographic_factors)
        service_score = self._percentage(
            [
                self._category_matches(service_corpus, self.service_alignment_signals[category])
                for category in self.service_alignment_signals
            ]
        )
        acquisition_pain_score = self._percentage(
            [
                self._category_matches(
                    acquisition_pain_corpus,
                    self.acquisition_pain_signals[category],
                )
                for category in self.acquisition_pain_signals
            ]
        )
        service_score = max(service_score, acquisition_pain_score)
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
                float(business_model != "unknown"),
                float(team_size_status != "unknown"),
                commercial_completeness / 100,
            )
        )
        dimension_completeness = {
            "firmographic_fit": firmographic_completeness,
            "service_alignment": self._percentage(
                (bool(service_corpus), bool(acquisition_pain_corpus))
            ),
            "buying_intent": 100.0 if intent_corpus else 0.0,
        }
        composite_score = round(
            firmographic_score * self.weight_firmographic_fit
            + service_score * self.weight_service_alignment
            + intent_score * self.weight_buying_intent,
            2,
        )

        evidence_gates = self._evidence_gates(account_data)
        firmographic_facts = {
            "industry": FirmographicFact(
                status=(
                    "matched"
                    if matched_industries
                    else "unknown"
                    if not industry_known
                    else "out_of_target"
                ),
                observed_value=industry_values or None,
            ),
            "geography": FirmographicFact(
                status=(
                    "matched"
                    if geography_match is True
                    else "out_of_target"
                    if geography_match is False
                    else "unknown"
                ),
                observed_value=geography_values or None,
            ),
            "business_model": FirmographicFact(
                status=(
                    "confirmed"
                    if has_b2b_customers
                    else "not_confirmed"
                    if business_model == "b2c"
                    else "unknown"
                ),
                observed_value=business_model,
            ),
            "employee_count": FirmographicFact(
                status=team_size_status,
                observed_value=next(
                    (
                        account_data.get(key)
                        for key in (
                            "employee_count",
                            "team_size_text",
                            "employee_range",
                            "team_size",
                        )
                        if account_data.get(key) is not None
                    ),
                    None,
                ),
            ),
            "commercial_maturity": FirmographicFact(
                status=(
                    "confirmed"
                    if commercial_maturity is True
                    else "not_confirmed"
                    if commercial_maturity is False
                    else "unknown"
                ),
                observed_value=commercial_maturity,
            ),
            "sales_infrastructure": FirmographicFact(
                status=(
                    "confirmed"
                    if sales_infrastructure is True
                    else "not_confirmed"
                    if sales_infrastructure is False
                    else "unknown"
                ),
                observed_value=sales_infrastructure,
            ),
            "acquisition_capacity": FirmographicFact(
                status=(
                    "confirmed"
                    if acquisition_capacity is True
                    else "not_confirmed"
                    if acquisition_capacity is False
                    else "unknown"
                ),
                observed_value=acquisition_capacity,
            ),
            "existing_sales_pipeline": FirmographicFact(
                status=(
                    "confirmed"
                    if existing_sales_pipeline is True
                    else "not_confirmed"
                    if existing_sales_pipeline is False
                    else "unknown"
                ),
                observed_value=existing_sales_pipeline,
            ),
        }
        disqualification_reasons = self._disqualification_reasons(
            account_data,
            industry_values,
            matched_industries,
            geography_values,
            geography_match,
            business_model,
            team_size_status,
        )
        core_data_complete = (
            industry_known
            and bool(geography_values)
            and business_model != "unknown"
            and team_size_status in ("in_range", "potentially_in_range")
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
            business_model=business_model,
            team_size_status=team_size_status,
            evidence_gates=evidence_gates,
            outreach_readiness=outreach_readiness,
            firmographic_facts=firmographic_facts,
            outreach_blockers=outreach_blockers,
            verified_decision_maker_emails=self._verified_decision_maker_emails(
                account_data
            ),
            matched_industries=matched_industries,
            matched_job_titles=matched_job_titles,
            matched_services=matched_services,
            matched_acquisition_pains=matched_acquisition_pains,
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

    def has_website_evidence(self, account_data: Mapping[str, Any]) -> bool:
        """Check for source URLs or successfully crawled website content."""
        return self._has_website_evidence(account_data)

    def has_business_activity_evidence(
        self, account_data: Mapping[str, Any]
    ) -> bool:
        """Check that the activity claim is supported by a successful same-site crawl."""
        return self._has_activity_evidence(account_data)

    def match_industries(self, values: Sequence[str]) -> list[str]:
        """Public industry matcher for enrichment and later qualification stages."""
        return self._match_industries(list(values))

    def matches_geography(self, values: Sequence[str]) -> bool | None:
        """Public geography matcher for enrichment and downstream validation."""
        return self._matches_geography(list(values))

    def _match_industries(self, values: list[str]) -> list[str]:
        matches: list[str] = []
        normalized_values = {
            self._normalize_industry(value) for value in values if value.strip()
        }
        for industry in self.target_industries:
            terms = self.industry_aliases.get(industry, []) + [industry]
            if normalized_values.intersection(
                self._normalize_industry(term) for term in terms
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
            if normalized.upper() in allowed:
                return True
        return False

    def classify_business_model(
        self,
        values: Sequence[str],
        customer_values: Sequence[str] = (),
    ) -> BusinessModel:
        """Classify only explicit model labels or evidence about actual customers."""
        return self._classify_business_model(
            list(values), list(customer_values)
        )

    @classmethod
    def _classify_business_model(
        cls,
        values: list[str],
        customer_values: list[str],
    ) -> BusinessModel:
        model_text = " ".join(cls._normalize(value) for value in values)
        model_text = re.sub(
            r"\b(?:not|never)\s+(?:exclusively\s+)?(?:b2b|b2c)\b",
            " ",
            model_text,
        )
        normalized_model = re.sub(r"[^a-z0-9]+", " ", model_text)
        normalized_model = " ".join(normalized_model.split())
        has_b2b = bool(
            re.search(r"\bb2b\b|\bbusiness to business\b", normalized_model)
        )
        has_b2c = bool(
            re.search(
                r"\bb2c\b|\bbusiness to consumer\b|\bconsumer only\b",
                normalized_model,
            )
        )

        normalized_customers = " ".join(
            re.sub(r"[^a-z0-9]+", " ", cls._normalize(value))
            for value in customer_values
        )
        normalized_customers = " ".join(normalized_customers.split())
        has_b2b = has_b2b or bool(
            re.search(
                r"\b(?:business(?:es)?|companies|organizations|enterprise customers?|"
                r"commercial clients?)\b|"
                r"\b(?:sell|serve|work with)\s+(?:to\s+)?"
                r"(?:business(?:es)?|companies|organizations)\b",
                normalized_customers,
            )
        )
        has_b2c = has_b2c or bool(
            re.search(
                r"\b(?:consumers?|individuals|households|consumer customers?)\b|"
                r"\b(?:sell|serve|work with)\s+(?:to\s+)?"
                r"(?:consumers?|individuals|households)\b",
                normalized_customers,
            )
        )
        if has_b2b and has_b2c:
            return "b2b_b2c"
        if has_b2b:
            return "b2b"
        if has_b2c:
            return "b2c"
        return "unknown"

    @staticmethod
    def is_verified_business_model_evidence(value: Any) -> bool:
        """Check that model evidence came from the company's verified description."""
        return (
            isinstance(value, Mapping)
            and value.get("source_type") == "verified_company_description"
        )

    def _team_size_status(
        self, data: Mapping[str, Any]
    ) -> Literal["in_range", "potentially_in_range", "out_of_range", "unknown"]:
        def within_range(count: int) -> bool:
            return count >= self.min_team_size and (
                self.max_team_size is None or count <= self.max_team_size
            )
        raw_count = data.get("employee_count")
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
            if not isfinite(raw_count) or not float(raw_count).is_integer() or raw_count < 0:
                return "unknown"
            count = int(raw_count)
            return "in_range" if within_range(count) else "out_of_range"
        if isinstance(raw_count, str) and raw_count.strip().isdigit():
            count = int(raw_count.strip())
            return "in_range" if within_range(count) else "out_of_range"
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
        if upper is not None and upper < lower:
            return "unknown"
        if upper is not None and upper < self.min_team_size:
            return "out_of_range"
        if self.max_team_size is not None and lower > self.max_team_size:
            if not (
                upper is not None
                and upper > lower
                and lower == self.max_team_size + 1
            ):
                return "out_of_range"
            return "potentially_in_range"
        if (
            lower >= self.min_team_size
            and upper is not None
            and (self.max_team_size is None or upper <= self.max_team_size)
        ):
            return "in_range"
        if upper is None and lower >= self.min_team_size:
            return "in_range" if self.max_team_size is None else "potentially_in_range"
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

    @classmethod
    def _has_commercial_maturity(cls, data: Mapping[str, Any]) -> bool | None:
        return cls._boolean_fact(
            data,
            (
                "proven_offer",
                "has_proven_offer",
                "established_product",
                "has_established_product",
                "established_service",
                "has_established_service",
                "product_service_established",
                "commercial_maturity",
                "annual_turnover_gbp_5m_plus",
            ),
        )

    @classmethod
    def _has_sales_infrastructure(cls, data: Mapping[str, Any]) -> bool | None:
        sales_process = cls._boolean_fact(
            data,
            (
                "has_internal_sales_team",
                "has_sales_team",
                "has_established_sales_team",
                "sales_process_defined",
                "sales_infrastructure",
            ),
        )
        crm_status = cls._boolean_fact(data, ("has_crm", "crm_in_use"))
        text_statuses = [
            cls._sales_infrastructure_text_status(value)
            for value in cls._values(
                data,
                "crm_system",
                "crm_name",
                "sales_pipeline_system",
                "lead_management_system",
            )
        ]
        if sales_process is True or crm_status is True or any(
            status is True for status in text_statuses
        ):
            return True
        if sales_process is False and crm_status is False:
            return False
        return None

    @classmethod
    def _has_acquisition_capacity(cls, data: Mapping[str, Any]) -> bool | None:
        return cls._boolean_fact(
            data,
            (
                "has_acquisition_budget",
                "meaningful_acquisition_budget",
                "acquisition_budget_confirmed",
                "has_marketing_budget",
                "marketing_budget_confirmed",
                "has_customer_acquisition_budget",
                "customer_acquisition_budget_confirmed",
            ),
        )

    @classmethod
    def _has_existing_sales_pipeline(cls, data: Mapping[str, Any]) -> bool | None:
        observed = cls._boolean_fact(
            data,
            (
                "has_existing_sales_pipeline",
                "sales_pipeline_exists",
                "active_sales_pipeline",
                "customer_acquisition_operation_active",
                "lead_generation_process_active",
                "outbound_program_active",
            ),
        )
        text_statuses = [
            cls._sales_pipeline_text_status(value)
            for value in cls._values(
                data,
                "sales_pipeline_evidence",
                "existing_sales_pipeline",
                "customer_acquisition_operation",
                "lead_generation_process",
                "outbound_program",
            )
        ]
        return cls._merge_boolean_facts([observed, *text_statuses])

    @staticmethod
    def _boolean_fact(
        data: Mapping[str, Any], keys: Sequence[str]
    ) -> bool | None:
        observed = [data[key] for key in keys if isinstance(data.get(key), bool)]
        if not observed or any(value != observed[0] for value in observed):
            return None
        return observed[0]

    @staticmethod
    def _merge_boolean_facts(values: Sequence[bool | None]) -> bool | None:
        observed = [value for value in values if isinstance(value, bool)]
        if not observed or any(value != observed[0] for value in observed):
            return None
        return observed[0]

    @staticmethod
    def _sales_infrastructure_text_status(value: str) -> bool | None:
        normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        if normalized in {
            "",
            "unknown",
            "not known",
            "not available",
            "not applicable",
            "unspecified",
            "pending",
            "n a",
            "na",
        }:
            return None
        if normalized in {"no", "none", "none yet", "untracked", "manual"}:
            return None
        if re.search(
            r"\b(?:no|without|not using|not in use|does not|doesn t|do not|don t)"
            r"\b.*\b(?:crm|pipeline|lead management|sales system)\b",
            normalized,
        ):
            return None

        known_systems = (
            "hubspot",
            "salesforce",
            "pipedrive",
            "zoho",
            "microsoft dynamics",
            "dynamics 365",
            "freshsales",
            "copper",
            "close crm",
            "sugarcrm",
            "creatio",
            "insightly",
            "keap",
            "attio",
        )
        if any(
            PearlLemonICP._contains_phrase(normalized, system)
            for system in known_systems
        ):
            return True
        if any(
            term in normalized
            for term in ("spreadsheet", "google sheets", "manual", "untracked")
        ):
            return None
        if any(
            PearlLemonICP._contains_phrase(normalized, phrase)
            for phrase in ("crm", "sales pipeline", "lead management")
        ):
            return True
        return None

    @staticmethod
    def _sales_pipeline_text_status(value: str) -> bool | None:
        normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        if normalized in {
            "",
            "unknown",
            "not known",
            "not available",
            "not applicable",
            "unspecified",
            "pending",
            "n a",
            "na",
        }:
            return None
        if re.search(
            r"\b(?:no|without|not using|not in use|does not|doesn t|do not|don t)"
            r"\b.*\b(?:sales pipeline|customer acquisition|lead generation|outbound)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:sales pipeline|customer acquisition operation|"
            r"lead generation process|outbound program|prospecting operation)\b",
            normalized,
        ):
            return True
        return None

    def _evidence_gates(self, data: Mapping[str, Any]) -> dict[str, bool]:
        identity = data.get("company_id") or data.get("company_name")
        domain = data.get("canonical_domain") or data.get("domain")
        declared_identity_state = data.get("company_identity_resolved")
        identity_resolved = (
            declared_identity_state
            if isinstance(declared_identity_state, bool)
            else (
                bool(identity)
                and self._valid_domain(domain)
                and str(identity).strip().casefold()
                not in {"unknown", "unresolved", "none"}
            )
        )
        website_evidence = self._has_website_evidence(data)
        activity_supported = self._has_activity_evidence(data)
        decision_maker_identified = self._has_decision_maker_evidence(data)
        contact_channel_verified = (
            data.get("contact_channel_verified") is True
            or data.get("verified_contact_channel") is True
            or data.get("contact_channel_verification") == "verified"
            or self._has_verified_contact(data.get("contact_channels"))
            or self._has_verified_email(data)
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
        if cls._valid_url(data.get("website")) and (
            cls._valid_url(data.get("website_source_url"))
            or cls._valid_url(data.get("website_evidence"))
            or cls._valid_url(data.get("website_evidence_url"))
        ):
            return True
        crawl = data.get("crawl_result")
        pages = cls._field_value(crawl, "pages")
        return isinstance(pages, Sequence) and any(
            cls._field_value(page, "fetch_status") == "success"
            and bool(str(cls._field_value(page, "cleaned_text") or "").strip())
            for page in pages
        )

    def _has_verified_email(self, data: Mapping[str, Any]) -> bool:
        return bool(self._verified_decision_maker_emails(data))

    def _verified_decision_maker_emails(
        self, data: Mapping[str, Any]
    ) -> list[str]:
        verified_emails = []
        for email, person in self._verified_email_people(data):
            title = str(
                self._field_value(person, "title")
                or self._field_value(person, "job_title")
                or ""
            )
            if self._is_target_job_title(title):
                verified_emails.append(email)
        return list(dict.fromkeys(verified_emails))

    @classmethod
    def _verified_email_people(
        cls, data: Mapping[str, Any]
    ) -> list[tuple[str, Any]]:
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
            return []
        candidates = cls._field_value(report, "email_candidates")
        if not isinstance(candidates, Sequence):
            return []
        decision_makers = data.get("decision_makers") or cls._field_value(
            data.get("enrichment_profile"), "decision_makers"
        )
        people = (
            list(decision_makers)
            if isinstance(decision_makers, Sequence)
            and not isinstance(decision_makers, (str, bytes))
            else []
        )
        matched: list[tuple[str, Any]] = []
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
            person = next(
                (
                    person
                    for person in people
                    if (
                        candidate_person_id
                        and candidate_person_id
                        == cls._field_value(person, "person_id")
                    )
                    or (
                        candidate_name
                        and candidate_name
                        == cls._normalize(
                            str(cls._field_value(person, "full_name") or "")
                        )
                    )
                ),
                None,
            )
            if person is not None:
                matched.append((email, person))
        return matched

    @classmethod
    def _has_activity_evidence(cls, data: Mapping[str, Any]) -> bool:
        evidence = data.get("business_activity_evidence")
        if isinstance(evidence, Mapping):
            evidence_mapping = cast(Mapping[str, Any], evidence)
            claim_value = evidence_mapping.get("claim")
            source_url = evidence_mapping.get("source_url")
            if (
                not isinstance(claim_value, str)
                or evidence_mapping.get("verification_status") != "verified"
            ):
                return False
            claim = claim_value.strip()
        elif isinstance(evidence, str):
            claim = evidence.strip()
            source_url = data.get("business_activity_source_url")
        else:
            return False
        if not isinstance(source_url, str):
            return False
        return (
            len(claim) >= 12
            and cls._normalize(claim) not in {"unverified", "unknown", "none", "pending"}
            and cls._valid_url(source_url)
            and cls._activity_claim_supported_by_crawl(data, claim, source_url)
        )

    @classmethod
    def _activity_claim_supported_by_crawl(
        cls, data: Mapping[str, Any], claim: str, source_url: str
    ) -> bool:
        crawl_result = data.get("crawl_result")
        pages_value = cls._field_value(crawl_result, "pages")
        if not isinstance(pages_value, Sequence) or isinstance(
            pages_value, (str, bytes)
        ):
            return False
        pages = cast(Sequence[Any], pages_value)

        source_host = cls._normalized_domain(source_url).removeprefix("www.")
        company_host = cls._normalized_domain(
            data.get("canonical_domain") or data.get("domain")
        ).removeprefix("www.")
        if not source_host or not company_host or not (
            source_host == company_host
            or source_host.endswith(f".{company_host}")
        ):
            return False

        source = urlsplit(source_url)
        source_path = source.path.rstrip("/").casefold() or "/"
        normalized_claim = cls._normalize(claim)
        for page in pages:
            if cls._field_value(page, "fetch_status") != "success":
                continue
            page_url = cls._field_value(page, "url")
            if not cls._valid_url(page_url):
                continue
            parsed_page = urlsplit(str(page_url))
            page_host = cls._normalized_domain(str(page_url)).removeprefix("www.")
            page_path = parsed_page.path.rstrip("/").casefold() or "/"
            page_text = cls._field_value(page, "cleaned_text")
            if (
                page_host == source_host
                and page_path == source_path
                and isinstance(page_text, str)
                and normalized_claim in cls._normalize(page_text)
            ):
                return True
        return False

    def _has_decision_maker_evidence(self, data: Mapping[str, Any]) -> bool:
        people = data.get("decision_makers") or self._field_value(
            data.get("enrichment_profile"), "decision_makers"
        )
        if not isinstance(people, Sequence) or isinstance(people, (str, bytes)):
            return False
        return any(
            bool(str(self._field_value(person, "full_name") or "").strip())
            and self._is_target_job_title(
                str(
                    self._field_value(person, "title")
                    or self._field_value(person, "job_title")
                    or ""
                )
            )
            and self._valid_url(self._field_value(person, "source_url"))
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
        try:
            parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        except ValueError:
            return ""
        return (parsed.hostname or "").rstrip(".").casefold()

    @classmethod
    def _valid_url(cls, value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parsed = urlsplit(value.strip())
        except ValueError:
            return False
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
        business_model: BusinessModel,
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
        if business_model == "b2c":
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
        classification_fields = (
            "company_name",
            "industry",
            "industries",
            "sector",
            "business_model",
            "company_type",
            "organization_type",
            "website_title",
        )
        values = self._values(data, *classification_fields)
        matches = []
        for keyword in self.blocked_keywords:
            normalized_keyword = self._normalize(keyword)
            matched = any(
                self._contains_phrase(value, normalized_keyword)
                and (
                    normalized_keyword != "recruiting agency"
                    or not (
                        self._contains_phrase(value, "software")
                        or self._contains_phrase(value, "platform")
                    )
                )
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

    @staticmethod
    def _normalize_industry(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


DEFAULT_PEARL_LEMON_ICP = PearlLemonICP()

__all__ = [
    "BusinessModel",
    "DEFAULT_PEARL_LEMON_ICP",
    "ICPMatchAssessment",
    "OutreachReadiness",
    "PearlLemonICP",
]
