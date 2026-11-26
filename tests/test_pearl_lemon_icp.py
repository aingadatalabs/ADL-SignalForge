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
        "proven_offer": True,
        "has_acquisition_budget": True,
        "has_existing_sales_pipeline": True,
        "business_activity_evidence": "Company website describes its workflow software.",
        "business_activity_source_url": "https://example.com/about",
        "crawl_result": {
            "canonical_domain": "example.com",
            "pages": [
                {
                    "url": "https://example.com/about",
                    "fetch_status": "success",
                    "cleaned_text": (
                        "Company website describes its workflow software."
                    ),
                }
            ],
        },
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
                cleaned_text=(
                    "Company website describes its workflow software."
                ),
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


def test_evidence_booleans_do_not_replace_source_evidence():
    account = qualified_account()
    account.pop("website_evidence")
    account.pop("business_activity_evidence")
    account.pop("business_activity_source_url")
    account.pop("decision_makers")
    account.pop("crawl_result")
    account["website_evidence_available"] = True
    account["business_activity_supported"] = True
    account["decision_maker_identified"] = True

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.evidence_gates["website_evidence_available"] is False
    assert assessment.evidence_gates["business_activity_supported"] is False
    assert assessment.evidence_gates["decision_maker_identified"] is False
    assert assessment.status == "pending"


@pytest.mark.parametrize(
    ("source_url", "page_url", "fetch_status", "page_text"),
    [
        ("https://example.com/about", "https://example.com/about", "success", None),
        (
            "https://third-party.example/about",
            "https://third-party.example/about",
            "success",
            "Company website describes its workflow software.",
        ),
        (
            "https://example.com/about",
            "https://example.com/about",
            "success",
            "A different claim about the company.",
        ),
        (
            "https://example.com/about",
            "https://example.com/about",
            "failed",
            "Company website describes its workflow software.",
        ),
    ],
)
def test_activity_claim_requires_matching_successful_same_site_crawl(
    source_url, page_url, fetch_status, page_text
):
    account = qualified_account()
    account["business_activity_source_url"] = source_url
    account["crawl_result"] = {
        "canonical_domain": "example.com",
        "pages": (
            []
            if page_text is None
            else [
                {
                    "url": page_url,
                    "fetch_status": fetch_status,
                    "cleaned_text": page_text,
                }
            ]
        ),
    }

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.evidence_gates["business_activity_supported"] is False
    assert assessment.status == "pending"


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
        ("DE", True),
        ("Remote - US/CA/DE", False),
        ("Working at a remote location", False),
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
        (1000, "out_of_range"),
        ("1-50", "potentially_in_range"),
        ("11-50", "in_range"),
        ("51-200", "in_range"),
        ("201-500", "potentially_in_range"),
        ("251-500", "potentially_in_range"),
        ("501-1000", "out_of_range"),
        ("50+", "potentially_in_range"),
        ("251+", "out_of_range"),
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
    elif expected_status == "unknown":
        assert assessment.status == "pending"
    elif expected_status == "potentially_in_range":
        assert assessment.status == "eligible"


@pytest.mark.parametrize(
    ("company_type", "reason"),
    [
        ("Recruiting agency", "blocked_keyword:recruiting_agency"),
        ("Job board", "blocked_keyword:job_board"),
    ],
)
def test_blocked_keywords_are_enforced_from_account_classification_fields(
    company_type, reason
):
    account = qualified_account()
    account["company_type"] = company_type

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "disqualified"
    assert reason in assessment.disqualification_reasons


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("description", "Unlike a recruiting agency, we help teams hire in-house."),
        ("website_content", "We are not a job board; our product manages workflows."),
        ("website_content", "We are not a government entity or non-profit."),
    ],
)
def test_blocked_entity_mentions_in_website_copy_do_not_disqualify(
    field, text
):
    account = qualified_account()
    account[field] = text

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert not any(
        reason.startswith("blocked_keyword:")
        for reason in assessment.disqualification_reasons
    )


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
    assert icp._match_industries(["Manufacturing"]) == ["Manufacturing"]
    assert icp._match_industries(["Commercial Property"]) == [
        "Property and Real Estate"
    ]
    assert icp._is_target_job_title("Founder Assistant") is False
    assert icp._is_target_job_title("Founder & CEO") is True
    assert icp._match_industries(["Fintech"]) == ["Financial Services"]


@pytest.mark.parametrize(
    "industry",
    [
        "consumer technology business",
        "cybersecurity consultancy",
        "company mentioning enterprise software",
        "recruitment",
        "e-commerce software provider",
    ],
)
def test_industry_match_requires_an_explicit_primary_industry_label(industry):
    assert PearlLemonICP().match_industries([industry]) == []


def test_primary_industry_takes_precedence_over_legacy_industry_fields():
    account = qualified_account()
    account["primary_industry"] = "Education"
    account["industry"] = "B2B SaaS"

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.matched_industries == []
    assert assessment.status == "disqualified"
    assert "industry_out_of_target" in assessment.disqualification_reasons


def test_legacy_industry_with_inferred_evidence_is_not_trusted():
    account = qualified_account()
    account["industry"] = ["E-commerce"]
    account["industry_evidence"] = [
        {
            "source_url": "https://example.com/about",
            "text": "Daktela provides contact-centre software for e-commerce.",
        }
    ]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.matched_industries == []
    assert assessment.firmographic_facts["industry"].status == "unknown"
    assert assessment.status == "pending"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Sales Director", True),
        ("Director of Marketing", True),
        ("Growth Director", True),
        ("Managing Director", True),
        ("Art Director", False),
        ("Customer Success Director", False),
        ("Warehouse Director", False),
        ("Director of Operations", False),
        ("Senior Director of IT Support", False),
        ("Associate Director", False),
    ],
)
def test_target_director_titles_are_limited_to_commercial_functions(
    title, expected
):
    assert PearlLemonICP()._is_target_job_title(title) is expected


def test_hybrid_business_model_is_eligible_for_b2b_and_contextual_mentions_are_not_exclusions():
    account = qualified_account()
    account["business_model"] = "B2B and B2C"
    account["description"] = "Provides software to government agencies."

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert assessment.business_model == "b2b_b2c"
    assert "excluded_industry" not in assessment.disqualification_reasons
    assert not any(reason.startswith("blocked_keyword:") for reason in assessment.disqualification_reasons)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("B2B", "b2b"),
        ("business to business", "b2b"),
        ("B2C", "b2c"),
        ("B2B and B2C", "b2b_b2c"),
        ("enterprise", "unknown"),
        ("book a demo", "unknown"),
        ("request a demo", "unknown"),
    ],
)
def test_business_model_classifier_requires_explicit_customer_model(
    value, expected
):
    assert PearlLemonICP().classify_business_model([value]) == expected


def test_customer_segment_evidence_can_confirm_business_customers():
    assert PearlLemonICP().classify_business_model(
        [], ["enterprise customers"]
    ) == "b2b"


def test_b2c_only_is_disqualified_and_unknown_business_model_stays_pending():
    account = qualified_account()
    account["business_model"] = "B2C"
    b2c_assessment = PearlLemonICP().evaluate(account)
    assert b2c_assessment.status == "disqualified"
    assert b2c_assessment.business_model == "b2c"

    account.pop("business_model")
    unknown_assessment = PearlLemonICP().evaluate(account)
    assert unknown_assessment.status == "pending"
    assert unknown_assessment.business_model == "unknown"


def test_legacy_unverified_business_model_evidence_is_not_trusted():
    account = qualified_account()
    account["business_model_evidence"] = {
        "source_url": "https://example.com/blog",
        "text": "Book a demo for enterprise partnerships.",
    }

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.business_model == "unknown"
    assert assessment.status == "pending"


def test_business_model_and_acquisition_alignment_are_independent():
    account = qualified_account()
    account.pop("acquisition_needs")

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.business_model == "b2b"
    assert assessment.dimension_scores["service_alignment"] == 0


def test_negated_service_need_is_not_awarded_service_alignment():
    account = qualified_account()
    account["acquisition_needs"] = ["We do not need cold email campaigns"]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.dimension_scores["service_alignment"] == 0


def test_acquisition_pain_signals_are_matched_and_prioritize_commercial_fit():
    account = qualified_account()
    account["commercial_challenges"] = [
        "Wrong target accounts and outdated outreach are hurting response rates.",
        "The team relies on manual prospect research and lacks repeatable qualification.",
        "This creates an unpredictable pipeline.",
    ]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.matched_acquisition_pains == [
        "poor target-account fit",
        "underperforming or outdated outreach",
        "inconsistent lead qualification",
        "unpredictable sales pipeline",
        "manual prospect research",
    ]
    assert assessment.dimension_scores["service_alignment"] == 100


@pytest.mark.parametrize(
    ("account", "expected_completeness"),
    [
        ({}, 0),
        ({"services_needed": ["cold email"]}, 50),
        ({"pain_points": ["wrong prospects"]}, 50),
        (
            {
                "services_needed": ["cold email"],
                "pain_points": ["wrong prospects"],
            },
            100,
        ),
    ],
)
def test_service_alignment_completeness_tracks_both_evidence_groups(
    account, expected_completeness
):
    assessment = PearlLemonICP().evaluate(account)

    assert assessment.dimension_completeness["service_alignment"] == expected_completeness


def test_negated_acquisition_pain_is_not_matched():
    account = qualified_account()
    account["pain_points"] = ["We do not need manual prospect research."]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.matched_acquisition_pains == []
    assert assessment.dimension_scores["service_alignment"] > 0


def test_commercial_readiness_and_existing_pipeline_are_priority_signals():
    account = qualified_account()
    for key in (
        "proven_offer",
        "has_internal_sales_team",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account.update(
        {
            "proven_offer": True,
            "has_acquisition_budget": True,
            "crm_system": "HubSpot",
        }
    )

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.firmographic_facts["commercial_maturity"].status == "confirmed"
    assert assessment.firmographic_facts["sales_infrastructure"].status == "confirmed"
    assert assessment.firmographic_facts["acquisition_capacity"].status == "confirmed"
    assert assessment.firmographic_facts["existing_sales_pipeline"].status == "unknown"
    assert assessment.status == "eligible"


@pytest.mark.parametrize(
    ("pipeline_value", "expected_status"),
    [
        ("no crm", "unknown"),
        ("none yet", "unknown"),
        ("untracked", "unknown"),
        ("Excel spreadsheet", "unknown"),
        ("manual", "unknown"),
        ("manual spreadsheet", "unknown"),
        ("n/a", "unknown"),
        ("HubSpot", "confirmed"),
        ("Custom CRM", "confirmed"),
        ("Snowflake and dbt", "unknown"),
    ],
)
def test_crm_evidence_only_confirms_sales_infrastructure(
    pipeline_value, expected_status
):
    account = qualified_account()
    for key in (
        "has_internal_sales_team",
        "proven_offer",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account["crm_system"] = pipeline_value

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.firmographic_facts["sales_infrastructure"].status == expected_status
    assert assessment.firmographic_facts["commercial_maturity"].status == "unknown"
    assert assessment.firmographic_facts["acquisition_capacity"].status == "unknown"
    assert assessment.firmographic_facts["existing_sales_pipeline"].status == "unknown"


@pytest.mark.parametrize(
    ("field", "expected_key"),
    [
        ("proven_offer", "commercial_maturity"),
        ("annual_turnover_gbp_5m_plus", "commercial_maturity"),
        ("has_internal_sales_team", "sales_infrastructure"),
        ("has_acquisition_budget", "acquisition_capacity"),
        ("has_existing_sales_pipeline", "existing_sales_pipeline"),
    ],
)
def test_commercial_facts_only_confirm_their_own_dimension(field, expected_key):
    account = qualified_account()
    for key in (
        "proven_offer",
        "has_internal_sales_team",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account[field] = True

    assessment = PearlLemonICP().evaluate(account)

    for fact_name in (
        "commercial_maturity",
        "sales_infrastructure",
        "acquisition_capacity",
        "existing_sales_pipeline",
    ):
        expected = "confirmed" if fact_name == expected_key else "unknown"
        assert assessment.firmographic_facts[fact_name].status == expected


def test_legacy_generic_commercial_capacity_does_not_confirm_specific_facts():
    account = qualified_account()
    for key in (
        "proven_offer",
        "has_internal_sales_team",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account["commercial_capacity"] = True

    assessment = PearlLemonICP().evaluate(account)

    for fact_name in (
        "commercial_maturity",
        "sales_infrastructure",
        "acquisition_capacity",
        "existing_sales_pipeline",
    ):
        assert assessment.firmographic_facts[fact_name].status == "unknown"


@pytest.mark.parametrize(
    ("pipeline_value", "expected_status"),
    [
        ("No sales pipeline", "not_confirmed"),
        ("Active sales pipeline", "confirmed"),
        ("Ongoing customer acquisition operation", "confirmed"),
        ("Salesforce", "unknown"),
        ("n/a", "unknown"),
    ],
)
def test_sales_pipeline_requires_evidence_of_an_active_operation(
    pipeline_value, expected_status
):
    account = qualified_account()
    for key in (
        "proven_offer",
        "has_internal_sales_team",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account["sales_pipeline_evidence"] = pipeline_value

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.firmographic_facts["existing_sales_pipeline"].status == (
        expected_status
    )


def test_generic_data_pipeline_does_not_confirm_sales_or_acquisition_operations():
    account = qualified_account()
    for key in (
        "proven_offer",
        "has_internal_sales_team",
        "has_acquisition_budget",
        "has_existing_sales_pipeline",
    ):
        account.pop(key)
    account["existing_data_pipeline"] = "Snowflake and dbt"

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.firmographic_facts["sales_infrastructure"].status == "unknown"
    assert assessment.firmographic_facts["acquisition_capacity"].status == "unknown"
    assert assessment.firmographic_facts["existing_sales_pipeline"].status == "unknown"


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


def test_non_target_role_does_not_satisfy_decision_maker_outreach_gate():
    account = qualified_account()
    account["decision_makers"] = [
        {
            "full_name": "Alex Smith",
            "title": "Sales Coordinator",
            "source_url": "https://example.com/team",
        }
    ]

    assessment = PearlLemonICP().evaluate(account)

    assert assessment.status == "eligible"
    assert not assessment.evidence_gates["decision_maker_identified"]
    assert assessment.outreach_readiness == "enrichment_required"


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
