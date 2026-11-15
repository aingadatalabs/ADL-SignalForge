import pytest
from pydantic import ValidationError

from agencies.pearl_lemon.icp import PearlLemonICP
from agencies.adl.enrichment_engine.schemas import (
    AgencyEnrichmentProfile,
    CrawledPage,
    CrawlResult,
    DecisionMaker,
    EmailCandidate,
    TechnologyEvidence,
    VerificationReport,
)


def qualified_account() -> dict[str, object]:
    return {
        "company_id": "company-1",
        "company_name": "Example Software",
        "canonical_domain": "example.com",
        "website": "https://example.com",
        "website_evidence": "https://example.com/about",
        "industry": "Enterprise Software",
        "country_code": "GB",
        "business_model": "B2B",
        "employee_count": 50,
        "has_internal_sales_team": True,
        "business_activity_evidence": "Company website describes its workflow software.",
        "business_activity_source_url": "https://example.com/about",
        "decision_makers": [
            {
                "full_name": "Alex Smith",
                "title": "CEO",
                "source_url": "https://example.com/about",
            }
        ],
        "contact_channel_verified": True,
        "acquisition_needs": ["cold email campaigns", "appointment setting"],
        "intent_signals": ["hiring sales development representatives"],
    }


def test_pearl_lemon_icp_qualifies_matching_account_with_verified_evidence():
    assessment = PearlLemonICP().evaluate(qualified_account())

    assert assessment.status == "eligible"
    assert assessment.dimension_scores["firmographic_fit"] == 100
    assert assessment.dimension_scores["service_alignment"] > 0
    assert assessment.dimension_scores["buying_intent"] > 0
    assert assessment.composite_score == round(
        assessment.dimension_scores["firmographic_fit"] * 0.4
        + assessment.dimension_scores["service_alignment"] * 0.3
        + assessment.dimension_scores["buying_intent"] * 0.3,
        2,
    )
    assert assessment.evidence_gates == {
        "company_identity_resolved": True,
        "website_evidence_available": True,
        "business_activity_supported": True,
        "decision_maker_identified": True,
        "contact_channel_verified_before_outreach": True,
    }
    assert assessment.outreach_readiness == "ready"
    assert assessment.dimension_completeness == {
        "firmographic_fit": 100,
        "service_alignment": 100,
        "buying_intent": 100,
    }
    assert PearlLemonICP().is_match(qualified_account())


def test_missing_contact_verification_preserves_icp_fit_but_requires_enrichment():
    account = qualified_account()
    account.pop("contact_channel_verified")

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert assessment.outreach_readiness == "enrichment_required"
    assert not assessment.evidence_gates["contact_channel_verified_before_outreach"]
    assert PearlLemonICP().is_match(account)


def test_excluded_industry_is_disqualified_even_when_other_fields_match():
    account = qualified_account()
    account["industry"] = "Government"

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "disqualified"
    assert "excluded_industry" in assessment.disqualification_reasons


def test_missing_firmographics_and_evidence_remain_pending():
    assessment = PearlLemonICP().evaluate({})

    assert assessment.status == "pending"
    assert assessment.composite_score == 0
    assert assessment.dimension_completeness == {
        "firmographic_fit": 0,
        "service_alignment": 0,
        "buying_intent": 0,
    }
    assert not any(assessment.evidence_gates.values())


def test_existing_enrichment_and_verification_models_supply_evidence():
    account = qualified_account()
    account.pop("website_evidence")
    account.pop("contact_channel_verified")
    account.pop("decision_makers")
    account["crawl_result"] = CrawlResult(
        company_id="company-1",
        canonical_domain="example.com",
        pages=[
            CrawledPage(
                url="https://example.com/about",
                path="/about",
                fetch_status="success",
                cleaned_text="Workflow software for businesses",
            )
        ],
    )
    decision_makers = [
        DecisionMaker(
            full_name="Alex Smith",
            title="CEO",
            source_url="https://example.com/about",
        )
    ]
    tech_stack = [
        TechnologyEvidence(
            name="HubSpot",
            category="crm",
            source_url="https://example.com",
            evidence="HubSpot script detected",
        )
    ]
    account["enrichment_profile"] = AgencyEnrichmentProfile(
        company_id="company-1",
        canonical_domain="example.com",
        decision_makers=decision_makers,
        tech_stack=tech_stack,
    )
    account["verification_report"] = VerificationReport(
        company_id="company-1",
        canonical_domain="example.com",
        email_candidates=[
            EmailCandidate(
                full_name="Alex Smith",
                email="alex@example.com",
                permutation_type="first",
                syntax_valid=True,
                domain_mail_status="mx_present",
                evidence_level="observed",
            )
        ],
    )

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert assessment.outreach_readiness == "ready"
    assert assessment.matched_job_titles == ["CEO"]
    assert assessment.matched_technologies == ["HubSpot"]


@pytest.mark.parametrize("industry", ["", " ", None])
def test_empty_industry_does_not_match_any_segment(industry):
    account = qualified_account()
    account["industry"] = industry

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.matched_industries == []
    assert assessment.status == "pending"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("London, UK", True),
        ("Paris, France", True),
        ("New York, NY, United States", True),
        ("Australia", True),
        ("Tokyo, Japan", False),
    ],
)
def test_geography_handles_city_country_strings(location, expected):
    assert PearlLemonICP()._matches_geography([location]) is expected


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        (5, "out_of_range"),
        (10, "in_range"),
        (85, "in_range"),
        (250, "in_range"),
        (251, "out_of_range"),
        ("1-50", "potentially_in_range"),
        ("251-500", "out_of_range"),
        ("50+", "potentially_in_range"),
        (None, "unknown"),
    ],
)
def test_team_size_ranges_preserve_uncertainty(value, expected_status):
    account = qualified_account()
    account["employee_count"] = value
    assessment = PearlLemonICP().evaluate(account)

    assert assessment.team_size_status == expected_status
    if expected_status == "out_of_range":
        assert assessment.status == "disqualified"
    elif expected_status in {"potentially_in_range", "unknown"}:
        assert assessment.status == "pending"


def test_blocked_keywords_are_enforced_from_account_classification_fields():
    account = qualified_account()
    account["company_type"] = "Recruiting agency"

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "disqualified"
    assert "blocked_keyword:recruiting_agency" in assessment.disqualification_reasons


def test_enrichment_pending_does_not_satisfy_decision_maker_gate():
    account = qualified_account()
    account.pop("decision_makers")
    account["enrichment_pending"] = True

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert assessment.outreach_readiness == "enrichment_required"
    assert not assessment.evidence_gates["decision_maker_identified"]


def test_nonempty_activity_text_without_source_is_not_verified_evidence():
    account = qualified_account()
    account.pop("business_activity_source_url")

    assessment = PearlLemonICP().evaluate(account)

    assert not assessment.evidence_gates["business_activity_supported"]
    assert assessment.status == "pending"


def test_placeholder_activity_claim_is_not_verified_by_a_source_url_alone():
    account = qualified_account()
    account["business_activity_evidence"] = "unverified"

    assessment = PearlLemonICP().evaluate(account)

    assert not assessment.evidence_gates["business_activity_supported"]
    assert assessment.status == "pending"


def test_industry_and_title_matchers_avoid_empty_and_substring_false_positives():
    icp = PearlLemonICP()
    assert icp._match_industries([""]) == []
    assert icp._match_industries(["Healthtech"]) == ["Healthcare"]
    assert icp._is_target_job_title("Founder Assistant") is False
    assert icp._is_target_job_title("Founder & CEO") is True
    assert icp._match_industries(["Fintech"]) == ["Financial Services"]


def test_hybrid_business_model_is_eligible_for_b2b_and_contextual_mentions_are_not_exclusions():
    account = qualified_account()
    account["business_model"] = "B2B and B2C"
    account["description"] = "Provides software to government agencies."

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert "excluded_industry" not in assessment.disqualification_reasons
    assert not any(reason.startswith("blocked_keyword:") for reason in assessment.disqualification_reasons)


def test_negated_service_need_is_not_awarded_service_alignment():
    account = qualified_account()
    account["acquisition_needs"] = ["We do not need cold email campaigns"]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.dimension_scores["service_alignment"] == 0


def test_score_prioritizes_an_eligible_company_without_becoming_a_hard_gate():
    account = qualified_account()
    account.pop("has_internal_sales_team")
    account.pop("acquisition_needs")
    account.pop("intent_signals")

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert assessment.outreach_readiness == "ready"
    assert assessment.dimension_scores["service_alignment"] == 0
    assert assessment.dimension_scores["buying_intent"] == 0
    assert assessment.dimension_completeness["service_alignment"] == 0
    assert assessment.dimension_completeness["buying_intent"] == 0


@pytest.mark.parametrize(
    "weights",
    [
        {"weight_firmographic_fit": -0.2, "weight_service_alignment": 0.6, "weight_buying_intent": 0.6},
        {"weight_firmographic_fit": float("nan")},
        {"weight_firmographic_fit": float("inf")},
    ],
)
def test_invalid_weights_are_rejected(weights):
    with pytest.raises(ValidationError):
        PearlLemonICP(**weights)


def test_unrelated_or_empty_email_candidate_does_not_verify_decision_maker():
    account = qualified_account()
    account.pop("contact_channel_verified")
    account["verification_report"] = VerificationReport(
        company_id="company-1",
        canonical_domain="example.com",
        email_candidates=[
            EmailCandidate(
                full_name="Someone Else",
                email="other@example.com",
                permutation_type="first",
                syntax_valid=True,
                domain_mail_status="mx_present",
                evidence_level="observed",
            )
        ],
    )

    assessment = PearlLemonICP().evaluate(account)

    assert not assessment.evidence_gates["contact_channel_verified_before_outreach"]
    assert assessment.outreach_readiness == "enrichment_required"


def test_email_report_for_another_company_does_not_verify_contact():
    account = qualified_account()
    account.pop("contact_channel_verified")
    account["verification_report"] = VerificationReport(
        company_id="different-company",
        canonical_domain="example.com",
        email_candidates=[
            EmailCandidate(
                full_name="Alex Smith",
                email="alex@example.com",
                permutation_type="first",
                syntax_valid=True,
                domain_mail_status="mx_present",
                evidence_level="observed",
            )
        ],
    )

    assessment = PearlLemonICP().evaluate(account)

    assert not assessment.evidence_gates["contact_channel_verified_before_outreach"]
    assert assessment.outreach_readiness == "enrichment_required"
