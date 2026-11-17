"""Pearl Lemon qualification, commercial scoring, and lead tiering."""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from agencies.adl.storage.jsonl_repository import JSONLRepository
from agencies.adl.qualification_engine.rules import (
    CompanyQualificationInput,
    DeterministicRuleEngine,
    QualificationConfig,
    RuleCheck,
)
from agencies.adl.qualification_engine.scorer import (
    QualificationScorer,
    ScorerConfig,
)
from agencies.adl.qualification_engine.tierer import LeadTierer, TiererConfig
from agencies.adl.qualification_engine.orchestrator import (
    QualificationOrchestrator,
)
from agencies.pearl_lemon.icp import (
    DEFAULT_PEARL_LEMON_ICP,
    FirmographicFact,
    ICPMatchAssessment,
    PearlLemonICP,
)

PEARL_LEMON_QUALIFICATION_VERSION = "1.0.0"
PEARL_LEMON_ICP_VERSION = "1.0.0"
PEARL_LEMON_SCORING_VERSION = "1.0.0"
DEFAULT_ENRICHED_RECORDS_PATH = (
    Path(__file__).parent / "enrichment_engine" / "enriched_records.jsonl"
)
DEFAULT_QUALIFIED_LEADS_PATH = (
    Path(__file__).parent / "qualified_leads" / "qualified_leads.jsonl"
)

QualificationStatus = Literal["eligible", "disqualified", "pending"]
LeadTier = Literal["tier_1", "tier_2", "tier_3"]
ScoreDimension = Literal[
    "firmographic_fit", "service_alignment", "buying_intent"
]
OutreachReadiness = Literal[
    "ready_for_review", "enrichment_required", "blocked"
]


class QualificationSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    status: QualificationStatus
    composite_score: float = Field(ge=0, le=100)
    tier: LeadTier
    outreach_readiness: OutreachReadiness


class ScoreEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    field: str
    source_url: str | None = None
    excerpt: str | None = None


class ScoreExplanation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    dimension: ScoreDimension
    score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=100)
    evidence_completeness: float = Field(ge=0, le=100)
    matched_signals: list[str] = Field(default_factory=list)
    evidence: list[ScoreEvidence] = Field(
        default_factory=lambda: list[ScoreEvidence]()
    )
    reason: str


class PearlLemonQualificationResult(BaseModel):
    """Auditable account qualification with independent score, tier, and readiness."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    company_id: str | None = None
    canonical_domain: str | None = None
    agency: Literal["pearl_lemon"] = "pearl_lemon"
    qualification: QualificationSummary
    dimension_scores: dict[str, float]
    dimension_completeness: dict[str, float]
    firmographic_facts: dict[str, FirmographicFact]
    score_explanation: list[ScoreExplanation]
    evidence_gates: dict[str, bool]
    matched_signals: dict[str, list[str]]
    disqualification_reasons: list[str]
    outreach_blockers: list[str]
    deterministic_checks: list[RuleCheck]
    input_warnings: list[str] = Field(default_factory=list)
    qualification_version: str
    icp_version: str
    scoring_version: str
    configuration_fingerprint: str
    pipeline_context: dict[str, Any] | None = None


def _default_pearl_lemon_scoring(icp: PearlLemonICP) -> ScorerConfig:
    """Map the Pearl Lemon ICP weights to the shared scorer configuration."""
    return ScorerConfig(
        weight_fit=icp.weight_firmographic_fit,
        weight_tech=icp.weight_service_alignment,
        weight_intent_access=icp.weight_buying_intent,
    )


@dataclass(frozen=True)
class PearlLemonQualificationSettings:
    """Immutable qualification config; ICP owns signal vocabulary, scorer owns weights."""

    eligibility: PearlLemonICP = field(
        default_factory=lambda: DEFAULT_PEARL_LEMON_ICP
    )
    scoring: ScorerConfig | None = None
    tiering: TiererConfig = field(default_factory=TiererConfig)
    qualification_version: str = PEARL_LEMON_QUALIFICATION_VERSION
    icp_version: str = PEARL_LEMON_ICP_VERSION
    scoring_version: str = PEARL_LEMON_SCORING_VERSION

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.qualification_version,
                self.icp_version,
                self.scoring_version,
            )
        ):
            raise ValueError("Qualification, ICP, and scoring versions are required")

        if self.scoring is None:
            scoring = _default_pearl_lemon_scoring(self.eligibility)
        else:
            self._validate_scoring_keywords(self.scoring)
            scoring = self.scoring
            configured_icp = PearlLemonICP.model_validate(
                {
                    **self.eligibility.model_dump(),
                    "weight_firmographic_fit": scoring.weight_fit,
                    "weight_service_alignment": scoring.weight_tech,
                    "weight_buying_intent": scoring.weight_intent_access,
                }
            )
            object.__setattr__(self, "eligibility", configured_icp)
        object.__setattr__(self, "scoring", scoring)
        # Reuse the shared tierer's threshold validation without coupling its
        # qualification-gated routing behavior to Pearl Lemon's score-only tier.
        LeadTierer(self.tiering)

    @staticmethod
    def _validate_scoring_keywords(scoring: ScorerConfig) -> None:
        defaults = ScorerConfig()
        keyword_fields = (
            "tier1_services",
            "tier2_services",
            "target_tech_keywords",
            "executive_titles",
        )
        if any(
            getattr(scoring, name) != getattr(defaults, name)
            for name in keyword_fields
        ):
            raise ValueError(
                "Pearl Lemon signal vocabulary is configured on the eligibility "
                "ICP, not in shared ScorerConfig keyword fields"
            )

    @property
    def scorer_config(self) -> ScorerConfig:
        if self.scoring is None:
            raise RuntimeError("Qualification settings were not initialized")
        return self.scoring

    @property
    def configuration_fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "qualification_version": self.qualification_version,
            "icp_version": self.icp_version,
            "scoring_version": self.scoring_version,
            "eligibility": self.eligibility.model_dump(mode="json"),
            "scoring": self.scorer_config.model_dump(mode="json"),
            "tiering": self.tiering.model_dump(mode="json"),
        }
        canonical_payload = _canonicalize_for_fingerprint(payload)
        serialized = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def create_orchestrator(self) -> "PearlLemonQualificationOrchestrator":
        return PearlLemonQualificationOrchestrator(self)


def _canonicalize_for_fingerprint(value: Any) -> Any:
    if isinstance(value, Mapping):
        typed_value = cast(Mapping[str, Any], value)
        return {
            key: _canonicalize_for_fingerprint(item)
            for key, item in sorted(typed_value.items())
        }
    if isinstance(value, list):
        typed_items = cast(list[Any], value)
        items = [_canonicalize_for_fingerprint(item) for item in typed_items]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    return value


class PearlLemonScorer:
    """Score the three Pearl Lemon dimensions through the canonical ICP evaluator."""

    def __init__(
        self,
        icp: PearlLemonICP,
    ) -> None:
        self.icp = icp

    def score(self, account: Mapping[str, Any]) -> ICPMatchAssessment:
        return self.icp.evaluate(account)


class PearlLemonTierer:
    """Assign a score-based tier without changing qualification or readiness."""

    def __init__(self, config: TiererConfig) -> None:
        LeadTierer(config)
        self.config = config

    def assign(self, score: float) -> LeadTier:
        if not 0 <= score <= 100 or not math.isfinite(score):
            raise ValueError("Score must be a finite value between 0 and 100")
        if score >= self.config.tier_1_threshold:
            return "tier_1"
        if score >= self.config.tier_2_threshold:
            return "tier_2"
        return "tier_3"


class PearlLemonQualificationOrchestrator:
    """Compose deterministic checks, Pearl Lemon ICP scoring, and score tiering."""

    def __init__(
        self,
        settings: PearlLemonQualificationSettings | None = None,
    ) -> None:
        self.settings = settings or DEFAULT_PEARL_LEMON_QUALIFICATION
        self.rule_engine = DeterministicRuleEngine(self.settings.eligibility)
        self.scorer = PearlLemonScorer(self.settings.eligibility)
        self.tierer = PearlLemonTierer(self.settings.tiering)

    async def evaluate_candidate(
        self,
        normalized_candidate: Any,
        enrichment_profile: Any | None = None,
        verification_report: Any | None = None,
    ) -> PearlLemonQualificationResult:
        """Evaluate without blocking the event loop on synchronous qualification work."""
        return await asyncio.to_thread(
            self.evaluate_candidate_sync,
            normalized_candidate,
            enrichment_profile,
            verification_report,
        )

    def evaluate_candidate_sync(
        self,
        normalized_candidate: Any,
        enrichment_profile: Any | None = None,
        verification_report: Any | None = None,
    ) -> PearlLemonQualificationResult:
        """Synchronously evaluate one company while preserving usable scores."""
        record = self._as_mapping(normalized_candidate)
        warnings: list[str] = []

        if enrichment_profile is not None:
            profile = self._as_mapping(enrichment_profile)
            record["enrichment_profile"] = profile
            for key, value in profile.items():
                if record.get(key) is None and value is not None:
                    record[key] = value
            if not any(
                record.get(key) is not None
                for key in (
                    "employee_count",
                    "team_size_text",
                    "employee_range",
                    "team_size",
                )
            ) and profile.get("estimated_team_size") is not None:
                record["team_size_text"] = profile["estimated_team_size"]
        if verification_report is not None:
            record["verification_report"] = self._as_mapping(verification_report)

        self._sanitize_team_size(record, warnings)
        raw_domains = (
            record.get("canonical_domain"),
            record.get("domain"),
            record.get("website"),
        )
        canonical_domain = self._canonical_domain(*raw_domains)
        if any(self._optional_string(value) for value in raw_domains) and not canonical_domain:
            warnings.append("Invalid company domain was treated as unresolved")
        if canonical_domain:
            record["canonical_domain"] = canonical_domain
        assessment = self.scorer.score(record)
        rule_result = self.rule_engine.evaluate(
            self._build_rule_input(record),
            verification=record.get("verification_report"),
        )
        status = self._qualification_status(assessment, rule_result.status)
        tier = self.tierer.assign(assessment.composite_score)
        outreach_readiness = self._outreach_readiness(assessment, status)
        checks = [*rule_result.checks]
        checks.extend(
            RuleCheck(
                rule_id=f"ICP-{reason.upper()}",
                rule_name="Pearl Lemon ICP disqualification",
                status="failed",
                reason=reason,
                observed_value=reason,
            )
            for reason in assessment.disqualification_reasons
        )
        reasons = list(assessment.disqualification_reasons)
        if rule_result.status == "disqualified":
            reasons.extend(rule_result.disqualification_reasons)
        reasons = list(dict.fromkeys(reasons))

        return PearlLemonQualificationResult(
            company_id=self._optional_identifier(
                record.get("company_id")
            )
            or self._optional_identifier(record.get("entity_id")),
            canonical_domain=canonical_domain,
            qualification=QualificationSummary(
                status=status,
                composite_score=assessment.composite_score,
                tier=tier,
                outreach_readiness=outreach_readiness,
            ),
            dimension_scores=assessment.dimension_scores,
            dimension_completeness=assessment.dimension_completeness,
            firmographic_facts=assessment.firmographic_facts,
            score_explanation=self._score_explanation(assessment, record),
            evidence_gates=assessment.evidence_gates,
            matched_signals={
                "industries": assessment.matched_industries,
                "decision_maker_titles": assessment.matched_job_titles,
                "services": assessment.matched_services,
                "intent": assessment.matched_intent_signals,
                "technologies": assessment.matched_technologies,
            },
            disqualification_reasons=reasons,
            outreach_blockers=self._outreach_blockers(assessment, status),
            deterministic_checks=checks,
            input_warnings=warnings,
            qualification_version=self.settings.qualification_version,
            icp_version=self.settings.icp_version,
            scoring_version=self.settings.scoring_version,
            configuration_fingerprint=self.settings.configuration_fingerprint,
            pipeline_context=(
                dict(cast(Mapping[str, Any], record["_pipeline_context"]))
                if isinstance(record.get("_pipeline_context"), Mapping)
                else None
            ),
        )

    @staticmethod
    def _as_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            typed_value = cast(Mapping[object, Any], value)
            return {
                key: item
                for key, item in typed_value.items()
                if isinstance(key, str)
            }
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                typed_dump = cast(Mapping[object, Any], dumped)
                return {
                    key: item
                    for key, item in typed_dump.items()
                    if isinstance(key, str)
                }
        raise TypeError("Qualification inputs must be mappings or Pydantic models")

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _optional_identifier(value: Any) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    def _canonical_domain(self, *values: Any) -> str | None:
        for value in values:
            candidate = self._optional_string(value)
            if not candidate or not self.settings.eligibility.is_valid_domain(candidate):
                continue
            try:
                parsed = urlsplit(
                    candidate if "://" in candidate else f"//{candidate}"
                )
            except ValueError:
                continue
            if parsed.hostname:
                return parsed.hostname.rstrip(".").casefold()
        return None

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            items: list[str] = []
            for item in cast(Sequence[object], value):
                if isinstance(item, str) and item.strip():
                    items.append(item.strip())
            return items
        return []

    @staticmethod
    def _sanitize_team_size(
        record: dict[str, Any],
        warnings: list[str],
    ) -> None:
        candidates = [
            record.get("employee_count"),
            record.get("team_size_text"),
            record.get("employee_range"),
            record.get("team_size"),
            record.get("estimated_team_size"),
        ]
        for value in candidates:
            if isinstance(value, bool):
                warnings.append("Boolean team-size data was ignored")
                PearlLemonQualificationOrchestrator._clear_team_size(record)
                return
            if isinstance(value, (int, float)):
                invalid_number = value < 0 or value > 1_000_000_000
                if isinstance(value, float):
                    invalid_number = invalid_number or not math.isfinite(value)
                if invalid_number:
                    warnings.append("Out-of-range numeric team-size data was ignored")
                    PearlLemonQualificationOrchestrator._clear_team_size(record)
                    return
            elif isinstance(value, str):
                if len(value) > 100 or any(
                    len(number) > 9 for number in re.findall(r"\d+", value)
                ):
                    warnings.append("Malformed team-size data was ignored")
                    PearlLemonQualificationOrchestrator._clear_team_size(record)
                    return
                numbers = re.findall(r"\d+", value)
                if len(numbers) > 1 and int(numbers[1]) < int(numbers[0]):
                    warnings.append("Inverted team-size range was treated as unknown")
                    PearlLemonQualificationOrchestrator._clear_team_size(record)
                    return
            elif value is not None:
                warnings.append("Unsupported team-size data was ignored")
                PearlLemonQualificationOrchestrator._clear_team_size(record)
                return

    @staticmethod
    def _clear_team_size(record: dict[str, Any]) -> None:
        for key in (
            "employee_count",
            "team_size_text",
            "employee_range",
            "team_size",
            "estimated_team_size",
        ):
            record.pop(key, None)

    def _build_rule_input(
        self, record: Mapping[str, Any]
    ) -> CompanyQualificationInput:
        company_id = (
            PearlLemonQualificationOrchestrator._optional_identifier(
                record.get("company_id")
            )
            or PearlLemonQualificationOrchestrator._optional_identifier(
                record.get("entity_id")
            )
            or "unknown"
        )
        company_name = (
            PearlLemonQualificationOrchestrator._optional_string(
                record.get("company_name") or record.get("agency_name")
            )
            or "Unknown"
        )
        domain = (
            PearlLemonQualificationOrchestrator._optional_string(
                record.get("canonical_domain")
                or record.get("domain")
                or record.get("website")
            )
            or ""
        )
        if domain and not self.settings.eligibility.is_valid_domain(domain):
            domain = ""
        employee_count = record.get("employee_count")
        team_size_text = (
            str(employee_count)
            if isinstance(employee_count, (int, float))
            and not isinstance(employee_count, bool)
            else PearlLemonQualificationOrchestrator._optional_string(
                record.get("team_size_text")
                or record.get("employee_range")
                or record.get("team_size")
                or record.get("estimated_team_size")
            )
        )
        services = PearlLemonQualificationOrchestrator._strings(
            record.get("acquisition_needs")
            or record.get("services_needed")
            or record.get("services")
        )
        description = (
            PearlLemonQualificationOrchestrator._optional_string(
                record.get("description")
                or record.get("business_activity_evidence")
            )
        )
        industry_values = list(
            dict.fromkeys(
                [
                    *PearlLemonQualificationOrchestrator._strings(
                        record.get("industry")
                    ),
                    *PearlLemonQualificationOrchestrator._strings(
                        record.get("industries")
                    ),
                    *PearlLemonQualificationOrchestrator._strings(
                        record.get("sector")
                    ),
                ]
            )
        )
        return CompanyQualificationInput(
            company_id=company_id,
            company_name=company_name,
            canonical_domain=domain,
            website=PearlLemonQualificationOrchestrator._optional_string(
                record.get("website")
            ),
            description=description,
            industry=industry_values[0] if industry_values else None,
            industries=industry_values,
            business_model=PearlLemonQualificationOrchestrator._optional_string(
                record.get("business_model") or record.get("market_type")
            ),
            company_type=PearlLemonQualificationOrchestrator._optional_string(
                record.get("company_type") or record.get("entity_type")
            ),
            organization_type=PearlLemonQualificationOrchestrator._optional_string(
                record.get("organization_type")
            ),
            services=services,
            country_code=PearlLemonQualificationOrchestrator._optional_string(
                record.get("country_code")
            ),
            team_size_text=team_size_text,
            is_unresolved_candidate=not bool(domain),
        )

    @staticmethod
    def _qualification_status(
        assessment: ICPMatchAssessment,
        rule_status: str,
    ) -> QualificationStatus:
        if assessment.status == "disqualified" or rule_status == "disqualified":
            return "disqualified"
        if assessment.status == "pending" or rule_status == "pending":
            return "pending"
        return "eligible"

    @staticmethod
    def _outreach_readiness(
        assessment: ICPMatchAssessment,
        qualification_status: QualificationStatus,
    ) -> OutreachReadiness:
        if qualification_status == "disqualified":
            return "blocked"
        if assessment.outreach_readiness != "ready":
            return "enrichment_required"
        return "ready_for_review"

    @staticmethod
    def _outreach_blockers(
        assessment: ICPMatchAssessment,
        qualification_status: QualificationStatus,
    ) -> list[str]:
        blockers = list(assessment.outreach_blockers)
        if qualification_status == "pending" and "company_qualification_pending" not in blockers:
            blockers.append("company_qualification_pending")
        if qualification_status == "disqualified" and "company_disqualified" not in blockers:
            blockers.append("company_disqualified")
        return blockers

    def _score_explanation(
        self,
        assessment: ICPMatchAssessment,
        record: Mapping[str, Any],
    ) -> list[ScoreExplanation]:
        signals = {
            "firmographic_fit": assessment.matched_industries,
            "service_alignment": assessment.matched_services,
            "buying_intent": assessment.matched_intent_signals,
        }
        evidence_fields: dict[str, tuple[str, ...]] = {
            "firmographic_fit": (
                "industry_evidence",
                "employee_count_evidence",
                "headquarters_evidence",
                "business_model_evidence",
                "commercial_capacity_evidence",
                "industry",
                "industries",
                "sector",
                "country_code",
                "country",
                "geography",
                "business_model",
                "market_type",
                "employee_count",
                "team_size_text",
                "employee_range",
                "team_size",
                "estimated_team_size",
                "commercial_capacity",
            ),
            "service_alignment": (
                "service_alignment_evidence",
                "service_evidence",
                "acquisition_needs",
                "commercial_challenges",
                "growth_needs",
                "services_needed",
                "sales_challenges",
            ),
            "buying_intent": (
                "intent_signal_evidence",
                "intent_evidence",
                "intent_signals",
                "recent_news",
                "job_openings",
                "announcements",
                "company_updates",
            ),
        }
        evidence = {
            dimension: self._collect_score_evidence(record, fields)
            for dimension, fields in evidence_fields.items()
        }
        scoring = self.settings.scorer_config
        weights = {
            "firmographic_fit": scoring.weight_fit,
            "service_alignment": scoring.weight_tech,
            "buying_intent": scoring.weight_intent_access,
        }
        return [
            ScoreExplanation(
                dimension=dimension,
                score=assessment.dimension_scores[dimension],
                weight=weights[dimension],
                contribution=round(
                    assessment.dimension_scores[dimension] * weights[dimension], 2
                ),
                evidence_completeness=assessment.dimension_completeness[dimension],
                matched_signals=signals[dimension],
                evidence=evidence[dimension],
                reason=(
                    f"{len(signals[dimension])} configured signal category "
                    f"matches; evidence completeness is "
                    f"{assessment.dimension_completeness[dimension]:.2f}%; "
                    f"{len(evidence[dimension])} supporting evidence item(s) recorded."
                ),
            )
            for dimension in (
                "firmographic_fit",
                "service_alignment",
                "buying_intent",
            )
        ]

    @classmethod
    def _collect_score_evidence(
        cls,
        record: Mapping[str, Any],
        fields: Sequence[str],
    ) -> list[ScoreEvidence]:
        collected: list[ScoreEvidence] = []
        seen: set[tuple[str, str | None, str | None]] = set()

        def append(
            field_name: str,
            source_url: Any = None,
            excerpt: Any = None,
        ) -> None:
            source = cls._optional_string(source_url)
            text = cls._optional_string(excerpt)
            if not source and not text:
                return
            item = (field_name, source, text[:500] if text else None)
            if item in seen or len(collected) >= 5:
                return
            seen.add(item)
            collected.append(
                ScoreEvidence(
                    field=field_name,
                    source_url=source,
                    excerpt=item[2],
                )
            )

        def visit(field_name: str, value: Any) -> None:
            if len(collected) >= 5 or value is None:
                return
            if isinstance(value, str):
                append(field_name, excerpt=value)
            elif isinstance(value, Mapping):
                typed_value = cast(Mapping[str, Any], value)
                source_url = (
                    typed_value.get("source_url")
                    or typed_value.get("source_reference")
                    or typed_value.get("url")
                )
                excerpt = (
                    typed_value.get("evidence")
                    or typed_value.get("text")
                    or typed_value.get("excerpt")
                    or typed_value.get("snippet")
                )
                source_urls = typed_value.get("source_urls")
                if isinstance(source_urls, Sequence) and not isinstance(
                    source_urls, (str, bytes)
                ):
                    for url in cast(Sequence[Any], source_urls):
                        append(field_name, source_url=url, excerpt=excerpt)
                append(field_name, source_url=source_url, excerpt=excerpt)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for item in cast(Sequence[Any], value):
                    visit(field_name, item)
                    if len(collected) >= 5:
                        break

        for field_name in fields:
            visit(field_name, record.get(field_name))
            if len(collected) >= 5:
                break
        return collected


def _read_enriched_records(path: str | Path) -> list[dict[str, Any]]:
    source_path = Path(path)
    records: list[dict[str, Any]] = []
    with source_path.open(encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in enriched records file {source_path} "
                    f"at line {line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Expected a JSON object in {source_path} at line {line_number}"
                )
            records.append(cast(dict[str, Any], record))
    return records


async def run_qualification(
    input_path: str | Path = DEFAULT_ENRICHED_RECORDS_PATH,
    output_path: str | Path = DEFAULT_QUALIFIED_LEADS_PATH,
    *,
    settings: PearlLemonQualificationSettings | None = None,
    max_concurrency: int = 4,
) -> list[PearlLemonQualificationResult]:
    """Qualify every enriched record and atomically save the result snapshot."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    source_records = _read_enriched_records(input_path)
    orchestrator = PearlLemonQualificationOrchestrator(settings)
    results: list[PearlLemonQualificationResult] = []
    for batch_start in range(0, len(source_records), max_concurrency):
        batch = source_records[batch_start : batch_start + max_concurrency]
        results.extend(
            await asyncio.gather(
                *(orchestrator.evaluate_candidate(record) for record in batch)
            )
        )

    repository = JSONLRepository(output_path, enable_locking=False)
    repository.overwrite(
        result.model_dump(mode="json") for result in results
    )
    return results


DEFAULT_PEARL_LEMON_QUALIFICATION = PearlLemonQualificationSettings()

SharedQualificationOrchestrator = QualificationOrchestrator
QualificationOrchestrator = PearlLemonQualificationOrchestrator

__all__ = [
    "DEFAULT_ENRICHED_RECORDS_PATH",
    "DEFAULT_PEARL_LEMON_QUALIFICATION",
    "DEFAULT_QUALIFIED_LEADS_PATH",
    "DeterministicRuleEngine",
    "LeadTierer",
    "PEARL_LEMON_ICP_VERSION",
    "PEARL_LEMON_QUALIFICATION_VERSION",
    "PEARL_LEMON_SCORING_VERSION",
    "PearlLemonQualificationOrchestrator",
    "PearlLemonQualificationResult",
    "PearlLemonQualificationSettings",
    "PearlLemonScorer",
    "PearlLemonTierer",
    "ScoreEvidence",
    "ScoreExplanation",
    "QualificationConfig",
    "QualificationOrchestrator",
    "QualificationScorer",
    "run_qualification",
    "ScorerConfig",
    "SharedQualificationOrchestrator",
    "TiererConfig",
]
