import asyncio
import json
import threading

import pytest
from pydantic import ValidationError

from agencies.adl.qualification_engine.scorer import ScorerConfig
from agencies.adl.qualification_engine.tierer import TiererConfig
from agencies.pearl_lemon.qualification import (
    DEFAULT_ENRICHED_RECORDS_PATH,
    DEFAULT_PEARL_LEMON_QUALIFICATION,
    DEFAULT_QUALIFIED_LEADS_PATH,
    PearlLemonQualificationOrchestrator,
    PearlLemonQualificationSettings,
    QualificationOrchestrator,
    run_qualification,
)


def qualified_account() -> dict[str, object]:
    return {
        "company_id": "company-001",
        "company_name": "Example Software",
        "canonical_domain": "example.com",
        "website": "https://example.com",
        "website_evidence_available": True,
        "industry": "B2B SaaS",
        "country_code": "GB",
        "business_model": "B2B",
        "employee_count": 50,
        "commercial_capacity": True,
        "business_activity_supported": True,
        "decision_makers": [
            {
                "full_name": "Alex Smith",
                "title": "CEO",
                "source_url": "https://example.com/team",
            }
        ],
        "contact_channel_verified": True,
        "acquisition_needs": [
            "prospect research",
            "cold email",
            "linkedin outreach",
        ],
        "intent_signals": [
            "sales hiring",
            "market expansion",
            "product launch",
        ],
    }


def test_default_settings_use_pearl_lemon_weights_and_targeting():
    settings = DEFAULT_PEARL_LEMON_QUALIFICATION

    assert QualificationOrchestrator is PearlLemonQualificationOrchestrator
    assert DEFAULT_ENRICHED_RECORDS_PATH.parent.name == "enrichment_engine"
    assert DEFAULT_ENRICHED_RECORDS_PATH.name == "enriched_records.jsonl"
    assert DEFAULT_QUALIFIED_LEADS_PATH.parent.name == "qualified_leads"
    assert DEFAULT_QUALIFIED_LEADS_PATH.name == "qualified_leads.jsonl"
    assert settings.eligibility.weight_firmographic_fit == 0.4
    assert settings.eligibility.weight_service_alignment == 0.3
    assert settings.eligibility.weight_buying_intent == 0.3
    assert settings.scorer_config.weight_fit == 0.4
    assert settings.tiering.tier_1_threshold == 80
    assert settings.tiering.tier_2_threshold == 50


def test_qualification_composes_rules_icp_score_tier_and_versions():
    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(
            qualified_account()
        )
    )

    assert result.qualification.status == "eligible"
    assert result.qualification.tier == "tier_2"
    assert result.qualification.outreach_readiness == "ready_for_review"
    assert result.qualification.composite_score == pytest.approx(67.86)
    assert result.dimension_scores == {
        "firmographic_fit": 100,
        "service_alignment": 50,
        "buying_intent": pytest.approx(42.86),
    }
    assert result.firmographic_facts["employee_count"].status == "in_range"
    assert result.firmographic_facts["commercial_capacity"].status == "confirmed"
    assert sum(item.contribution for item in result.score_explanation) == pytest.approx(
        result.qualification.composite_score
    )
    assert result.evidence_gates["contact_channel_verified_before_outreach"]
    assert result.qualification_version == "1.0.0"
    assert result.icp_version == "1.0.0"
    assert result.scoring_version == "1.0.0"
    assert len(result.configuration_fingerprint) == 64


def test_description_keyword_does_not_disqualify_structured_b2b_company():
    account = qualified_account()
    account["description"] = (
        "Our B2B software helps customers with government procurement."
    )

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "eligible"
    assert not any(check.rule_id == "R003" and check.status == "failed"
                   for check in result.deterministic_checks)


def test_reversed_team_size_range_is_pending_and_warned():
    account = qualified_account()
    account["employee_count"] = None
    account["team_size_text"] = "250-10"

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "eligible"
    assert any("Inverted team-size" in w for w in result.input_warnings)
    assert result.firmographic_facts["employee_count"].status == "unknown"
    assert "Inverted team-size range was treated as unknown" in result.input_warnings


def test_deterministic_disqualification_is_in_summary_reasons():
    account = qualified_account()
    account["canonical_domain"] = "g2.com"
    account["domain"] = "g2.com"
    account["website"] = "https://g2.com"

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "disqualified"
    assert "blocklisted_domain" in result.disqualification_reasons


def test_enrichment_profile_fills_none_team_size_and_decision_makers():
    account = qualified_account()
    account["employee_count"] = None
    account["decision_makers"] = None

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(
            account,
            enrichment_profile={
                "estimated_team_size": "25-100 employees",
                "decision_makers": [
                    {
                        "full_name": "Alex Smith",
                        "title": "CEO",
                        "source_url": "https://example.com/team",
                    }
                ],
            },
        )
    )

    assert result.qualification.status == "eligible"
    assert result.firmographic_facts["employee_count"].status == "in_range"


def test_numeric_company_id_and_website_url_are_canonicalized():
    account = qualified_account()
    account["company_id"] = 123
    account["canonical_domain"] = None
    account.pop("domain", None)
    account["website"] = "https://www.example.com/about"

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.company_id == "123"
    assert result.canonical_domain == "www.example.com"
    assert result.qualification.status == "eligible"


def test_custom_icp_weights_survive_when_scoring_is_omitted():
    icp = PearlLemonQualificationSettings().eligibility.model_copy(
        update={
            "weight_firmographic_fit": 0.2,
            "weight_service_alignment": 0.3,
            "weight_buying_intent": 0.5,
        }
    )

    settings = PearlLemonQualificationSettings(eligibility=icp)

    assert settings.eligibility.weight_firmographic_fit == 0.2
    assert settings.scorer_config.weight_fit == 0.2
    assert (
        settings.configuration_fingerprint
        != DEFAULT_PEARL_LEMON_QUALIFICATION.configuration_fingerprint
    )


def test_custom_scoring_keywords_are_rejected_as_unsupported():
    with pytest.raises(ValueError, match="signal vocabulary"):
        PearlLemonQualificationSettings(
            scoring=ScorerConfig(tier1_services=["custom service"])
        )


def test_score_explanation_includes_source_and_excerpt():
    account = qualified_account()
    account["industry_evidence"] = [
        {
            "source_url": "https://example.com/about",
            "text": "B2B SaaS workflow automation provider",
        }
    ]

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    firmographic = next(
        item for item in result.score_explanation
        if item.dimension == "firmographic_fit"
    )
    assert firmographic.evidence[0].source_url == "https://example.com/about"
    assert "B2B SaaS workflow automation" in firmographic.evidence[0].excerpt


def test_async_entrypoint_does_not_block_event_loop(monkeypatch):
    orchestrator = PearlLemonQualificationOrchestrator()
    worker_started = threading.Event()
    release_worker = threading.Event()
    expected = object()

    def blocked_evaluation(*_args):
        worker_started.set()
        release_worker.wait(timeout=2)
        return expected

    monkeypatch.setattr(orchestrator, "evaluate_candidate_sync", blocked_evaluation)

    async def run():
        task = asyncio.create_task(orchestrator.evaluate_candidate({}))
        await asyncio.sleep(0)
        assert await asyncio.to_thread(worker_started.wait, 1)
        release_worker.set()
        return await task

    assert asyncio.run(run()) is expected


def test_run_qualification_reads_enriched_jsonl_and_writes_all_outcomes(tmp_path):
    input_path = tmp_path / "enrichment_engine" / "enriched_records.jsonl"
    output_path = tmp_path / "qualified_leads" / "qualified_leads.jsonl"
    input_path.parent.mkdir(parents=True)
    input_path.write_text(
        json.dumps(qualified_account()) + "\n"
        + json.dumps({"company_id": "incomplete"}) + "\n",
        encoding="utf-8",
    )

    results = asyncio.run(run_qualification(input_path, output_path))

    persisted = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [result.qualification.status for result in results] == [
        "eligible",
        "pending",
    ]
    assert [record["qualification"]["status"] for record in persisted] == [
        "eligible",
        "pending",
    ]


def test_run_qualification_evaluates_in_bounded_ordered_batches(
    tmp_path, monkeypatch
):
    input_path = tmp_path / "enriched_records.jsonl"
    output_path = tmp_path / "qualified_records.jsonl"
    records = []
    for index in range(4):
        record = qualified_account()
        record["company_id"] = f"company-{index}"
        records.append(record)
    input_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    original_evaluate = PearlLemonQualificationOrchestrator.evaluate_candidate
    active = 0
    max_active = 0

    async def tracked_evaluate(self, record):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return await original_evaluate(self, record)
        finally:
            active -= 1

    monkeypatch.setattr(
        PearlLemonQualificationOrchestrator,
        "evaluate_candidate",
        tracked_evaluate,
    )

    results = asyncio.run(
        run_qualification(
            input_path,
            output_path,
            max_concurrency=2,
        )
    )

    assert max_active == 2
    assert [result.company_id for result in results] == [
        "company-0",
        "company-1",
        "company-2",
        "company-3",
    ]


def test_run_qualification_rejects_malformed_input_jsonl(tmp_path):
    input_path = tmp_path / "enriched_records.jsonl"
    input_path.write_text("{}\nnot-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"at line 2"):
        asyncio.run(run_qualification(input_path, tmp_path / "qualified.jsonl"))


def test_disqualified_company_keeps_commercial_score_and_score_based_tier():
    account = qualified_account()
    account["industry"] = "Government"
    account["acquisition_needs"] = [
        "prospect research",
        "cold email",
        "linkedin outreach",
        "appointment setting",
        "outbound calling",
        "customer acquisition",
    ]
    account["intent_signals"] = [
        "sales hiring",
        "market expansion",
        "product launch",
        "new target market",
        "funding round",
        "new head of sales",
        "sales operations",
    ]

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "disqualified"
    assert result.qualification.composite_score >= 80
    assert result.qualification.tier == "tier_1"
    assert result.qualification.outreach_readiness == "blocked"
    assert "excluded_industry" in result.disqualification_reasons


def test_missing_evidence_is_pending_without_suppressing_commercial_score():
    account = qualified_account()
    account.pop("website_evidence_available")

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "pending"
    assert result.qualification.composite_score > 0
    assert result.qualification.outreach_readiness == "enrichment_required"
    assert not result.evidence_gates["website_evidence_available"]


def test_explicit_unresolved_identity_is_not_overridden_by_name_and_domain():
    account = qualified_account()
    account["company_identity_resolved"] = False

    result = asyncio.run(
        PearlLemonQualificationOrchestrator().evaluate_candidate(account)
    )

    assert result.qualification.status == "pending"
    assert not result.evidence_gates["company_identity_resolved"]
    assert result.firmographic_facts["commercial_capacity"].status == "confirmed"


@pytest.mark.parametrize(
    ("weight_fit", "weight_service", "weight_intent", "changes", "expected_tier"),
    [
        (1.0, 0.0, 0.0, {"commercial_capacity": None}, "tier_1"),
        (0.0, 1.0, 0.0, {}, "tier_2"),
    ],
)
def test_tier_boundaries_include_exactly_80_and_50(
    weight_fit,
    weight_service,
    weight_intent,
    changes,
    expected_tier,
):
    account = qualified_account()
    account.pop("commercial_capacity")
    account.update(changes)
    settings = PearlLemonQualificationSettings(
        scoring=ScorerConfig(
            weight_fit=weight_fit,
            weight_tech=weight_service,
            weight_intent_access=weight_intent,
        )
    )

    result = asyncio.run(
        settings.create_orchestrator().evaluate_candidate(account)
    )

    expected_score = 80 if expected_tier == "tier_1" else 50
    assert result.qualification.composite_score == expected_score
    assert result.qualification.tier == expected_tier
    if weight_fit == 1.0:
        assert result.firmographic_facts["commercial_capacity"].status == "unknown"


def test_empty_and_malformed_enrichment_data_returns_pending_not_an_exception():
    orchestrator = PearlLemonQualificationOrchestrator()

    empty = asyncio.run(orchestrator.evaluate_candidate({}))
    malformed = asyncio.run(
        orchestrator.evaluate_candidate(
            {
                **qualified_account(),
                "employee_count": "999999999999999999999999999999",
            }
        )
    )
    huge_integer = asyncio.run(
        orchestrator.evaluate_candidate(
            {**qualified_account(), "employee_count": 10**1000}
        )
    )
    malformed_domain = asyncio.run(
        orchestrator.evaluate_candidate(
            {
                **qualified_account(),
                "canonical_domain": "http://[",
                "domain": "http://[",
                "website": "http://[",
            }
        )
    )

    assert empty.qualification.status == "pending"
    assert empty.qualification.tier == "tier_3"
    assert malformed.qualification.status == "eligible"
    assert any("Malformed team-size" in w for w in malformed.input_warnings)
    assert malformed.input_warnings == ["Malformed team-size data was ignored"]
    assert huge_integer.qualification.status == "eligible"
    assert huge_integer.input_warnings == [
        "Out-of-range numeric team-size data was ignored"
    ]
    assert malformed_domain.qualification.status == "pending"
    assert malformed_domain.canonical_domain is None
    assert malformed_domain.input_warnings == [
        "Invalid company domain was treated as unresolved"
    ]


def test_invalid_weight_and_tier_threshold_configs_are_rejected():
    with pytest.raises(ValidationError):
        ScorerConfig(weight_fit=0.2, weight_tech=0.2, weight_intent_access=0.2)

    with pytest.raises(ValueError, match="strictly greater"):
        PearlLemonQualificationSettings(
            tiering=TiererConfig(tier_1_threshold=50, tier_2_threshold=50)
        )
