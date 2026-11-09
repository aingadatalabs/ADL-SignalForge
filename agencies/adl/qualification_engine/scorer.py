import logging
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("signalforge.qualification.scorer")


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    fit_score: float = Field(description="Normalized 0-100 score for agency service fit")
    tech_score: float = Field(description="Normalized 0-100 score for tech stack alignment & opportunity")
    intent_access_score: float = Field(description="Normalized 0-100 score for intent signals & decision-maker access")
    composite_score: float = Field(description="Final weighted score between 0 and 100")
    evidence_coverage: float = Field(description="Percentage (0-100) indicating data completeness and trust")
    scoring_notes: list[str] = Field(default_factory=list)


class ScorerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    weight_fit: float = Field(default=0.40, ge=0.0, le=1.0)
    weight_tech: float = Field(default=0.30, ge=0.0, le=1.0)
    weight_intent_access: float = Field(default=0.30, ge=0.0, le=1.0)

    tier1_services: list[str] = Field(
        default_factory=lambda: [
            "b2b lead generation",
            "cold outreach",
            "outbound sales",
            "demand generation",
            "appointment setting",
            "sales development",
        ]
    )
    tier2_services: list[str] = Field(
        default_factory=lambda: [
            "digital marketing",
            "performance marketing",
            "seo agency",
            "ppc management",
            "growth marketing",
        ]
    )
    target_tech_keywords: list[str] = Field(
        default_factory=lambda: [
            "apollo",
            "instantly",
            "smartlead",
            "clay",
            "lemlist",
            "gohighlevel",
            "hubspot",
            "salesforce",
        ]
    )
    executive_titles: list[str] = Field(
        default_factory=lambda: [
            "founder",
            "ceo",
            "cto",
            "owner",
            "managing director",
            "head of growth",
            "partner",
        ]
    )

    @model_validator(mode="after")
    def validate_weights(self) -> "ScorerConfig":
        total = self.weight_fit + self.weight_tech + self.weight_intent_access
        if abs(total - 1.0) > 1e-5:
            raise ValueError(f"Scoring weights must sum to 1.0, got {total}")
        return self


class QualificationScorer:
    def __init__(self, config: ScorerConfig | None = None):
        self.config = config or ScorerConfig()

    def score(self, profile: Any, verification: Any | None = None) -> ScoreBreakdown:
        """
        Calculates a transparent, evidence-based composite score (0-100) 
        across ICP Fit, Tech Stack Alignment, and Intent/Access.
        """
        notes: list[str] = []
        coverage_factors = 0
        max_coverage_factors = 3  # Fit, Tech, Intent/Access

        # 1. Calculate ICP Service Fit (40%)
        fit_score, fit_has_data = self._calculate_fit_score(profile, notes)
        if fit_has_data:
            coverage_factors += 1

        # 2. Calculate Tech Alignment & Infrastructure Opportunity (30%)
        tech_score, tech_has_data = self._calculate_tech_score(profile, notes)
        if tech_has_data:
            coverage_factors += 1

        # 3. Calculate Intent & Decision-Maker Access (30%)
        intent_score, intent_has_data = self._calculate_intent_score(profile, verification, notes)
        if intent_has_data:
            coverage_factors += 1

        # Calculate evidence coverage percentage
        evidence_coverage = round((coverage_factors / max_coverage_factors) * 100.0, 2)

        # Composite weighted score calculation
        composite = (
            (fit_score * self.config.weight_fit) +
            (tech_score * self.config.weight_tech) +
            (intent_score * self.config.weight_intent_access)
        )
        composite_rounded = round(max(0.0, min(100.0, composite)), 2)

        return ScoreBreakdown(
            fit_score=round(fit_score, 2),
            tech_score=round(tech_score, 2),
            intent_access_score=round(intent_score, 2),
            composite_score=composite_rounded,
            evidence_coverage=evidence_coverage,
            scoring_notes=notes,
        )

    def _normalize_text(self, text: str) -> str:
        """Cleans extracted text by stripping HTML fragments, line breaks, and collapsing whitespace."""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip().lower()

    def _calculate_fit_score(self, profile: Any, notes: list[str]) -> tuple[float, bool]:
        extracted_meta = getattr(profile, "extraction_metadata", {}) or {}
        raw_services = extracted_meta.get("services", []) or []
        raw_summary = extracted_meta.get("summary", "") or extracted_meta.get("description", "") or ""

        if not raw_services and not raw_summary:
            notes.append("ICP Fit: Insufficient service extraction evidence; assigning neutral baseline score.")
            return 50.0, False

        services_normalized = [self._normalize_text(s) for s in raw_services]
        summary_normalized = self._normalize_text(raw_summary)
        corpus = f"{summary_normalized} {' '.join(services_normalized)}"

        # Check for Tier 1 explicit outbound services
        tier1_matches = [kw for kw in self.config.tier1_services if kw in corpus]
        if len(tier1_matches) >= 2:
            notes.append(f"ICP Fit: Strong primary match with multiple Tier 1 services: {tier1_matches}")
            return 100.0, True
        elif len(tier1_matches) == 1:
            notes.append(f"ICP Fit: Solid primary match with Tier 1 service: {tier1_matches}")
            return 85.0, True

        # Check for Tier 2 secondary marketing services
        tier2_matches = [kw for kw in self.config.tier2_services if kw in corpus]
        if tier2_matches:
            notes.append(f"ICP Fit: Secondary match with digital/marketing services: {tier2_matches}")
            return 60.0, True

        notes.append("ICP Fit: Services extracted but do not align with core ADL target categories.")
        return 30.0, True

    def _calculate_tech_score(self, profile: Any, notes: list[str]) -> tuple[float, bool]:
        tech_stack = getattr(profile, "tech_stack", []) or []
        if not tech_stack:
            notes.append("Tech Alignment: No technical stack evidence recorded; assigning neutral baseline.")
            return 50.0, False

        detected_tech = []
        for tech in tech_stack:
            name = getattr(tech, "name", "").lower()
            if any(target in name for target in self.config.target_tech_keywords):
                detected_tech.append(name)

        unique_tech = sorted(list(set(detected_tech)))
        if unique_tech:
            notes.append(f"Tech Alignment: Detected target outbound/CRM tools: {unique_tech}")
            score = min(100.0, 50.0 + (len(unique_tech) * 15.0))
            return score, True

        notes.append("Tech Alignment: Technical stack recorded, but no core outbound/CRM tools identified.")
        return 40.0, True

    def _calculate_intent_score(self, profile: Any, verification: Any | None, notes: list[str]) -> tuple[float, bool]:
        decision_makers = getattr(profile, "decision_makers", []) or []
        if not decision_makers:
            notes.append("Intent & Access: No decision-makers identified.")
            return 20.0, False

        has_executive = False
        valid_verified_email_count = 0

        # Build verified email mapping from verification report (ensuring observed status)
        verified_emails_map = {}
        if verification:
            candidates = getattr(verification, "email_candidates", [])
            for c in candidates:
                if getattr(c, "evidence_level", "") == "observed":
                    email = getattr(c, "email", "").lower().strip()
                    if email:
                        verified_emails_map[email] = True

        for dm in decision_makers:
            title = getattr(dm, "title", "").lower()
            corporate_email = getattr(dm, "corporate_email", None)

            if any(t in title for t in self.config.executive_titles):
                has_executive = True

            if corporate_email:
                clean_email = corporate_email.lower().strip()
                # Exclude generic administrative prefixes from counting as personal contact proof
                is_generic = any(clean_email.startswith(p) for p in ["info@", "support@", "contact@", "hello@"])
                if not is_generic and (clean_email in verified_emails_map or getattr(dm, "confidence", 0.0) >= 0.85):
                    valid_verified_email_count += 1

        score = 25.0  # Baseline for extracted decision makers
        
        if has_executive:
            score += 45.0
            notes.append("Intent & Access: Confirmed C-level or Founder stakeholder present.")
        
        if valid_verified_email_count > 0:
            score += 30.0
            notes.append(f"Intent & Access: {valid_verified_email_count} verified, attributable decision-maker email(s) confirmed.")

        return min(100.0, score), True