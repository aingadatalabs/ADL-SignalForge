import importlib
from pathlib import Path


AGENCIES = ("adl", "pearl_lemon", "cience", "fiteat", "elimuflow")
AGENCY_MODULES = (
    "icp",
    "normalizer",
    "normalization",
    "dedup",
    "qualification",
    "enrichment",
)
AGENCY_PACKAGES = (
    "enrichment_engine",
    "extraction",
    "identity",
    "persistence",
    "qualification_engine",
    "storage",
)


def test_each_agency_exposes_requested_modules():
    for agency in AGENCIES:
        for module in AGENCY_MODULES:
            imported = importlib.import_module(f"agencies.{agency}.{module}")
            assert Path(imported.__file__).is_file()

    for agency in AGENCIES:
        for package in AGENCY_PACKAGES:
            imported = importlib.import_module(f"agencies.{agency}.{package}")
            assert Path(imported.__file__).name == "__init__.py"


def test_each_agency_has_a_pipeline_entrypoint():
    for agency in AGENCIES:
        pipeline = importlib.import_module(f"agencies.{agency}.run_pipeline")
        assert callable(pipeline.run_pipeline)


def test_adl_qualification_facade_preserves_nested_and_legacy_imports():
    facade = importlib.import_module("agencies.adl.qualification")
    engine_rules = importlib.import_module("agencies.adl.qualification.rules")
    legacy_rules = importlib.import_module("qualification.rules")

    assert Path(facade.__file__).name == "qualification.py"
    assert hasattr(facade, "DEFAULT_ADL_QUALIFICATION")
    assert Path(engine_rules.__file__) == Path(legacy_rules.__file__)
    assert engine_rules.QualificationConfig.__name__ == legacy_rules.QualificationConfig.__name__


def test_adl_enrichment_facade_preserves_nested_and_legacy_imports():
    facade = importlib.import_module("agencies.adl.enrichment")
    engine_schemas = importlib.import_module("agencies.adl.enrichment.schemas")
    legacy_schemas = importlib.import_module("enrichment.schemas")

    assert Path(facade.__file__).name == "enrichment.py"
    assert Path(engine_schemas.__file__) == Path(legacy_schemas.__file__)


def test_agency_qualification_defaults_use_shared_baselines():
    defaults = (
        importlib.import_module(
            "agencies.adl.qualification"
        ).DEFAULT_ADL_QUALIFICATION,
        importlib.import_module(
            "agencies.pearl_lemon.qualification"
        ).DEFAULT_PEARL_LEMON_QUALIFICATION,
        importlib.import_module(
            "agencies.cience.qualification"
        ).DEFAULT_CIENCE_QUALIFICATION,
    )

    for settings in defaults:
        assert settings.scoring.weight_fit == 0.4
        assert settings.scoring.weight_tech == 0.3
        assert settings.scoring.weight_intent_access == 0.3
        assert settings.tiering.tier_1_threshold > settings.tiering.tier_2_threshold


def test_fiteat_and_elimuflow_qualification_defaults_use_shared_baselines():
    defaults = (
        importlib.import_module(
            "agencies.fiteat.qualification"
        ).DEFAULT_FITEAT_QUALIFICATION,
        importlib.import_module(
            "agencies.elimuflow.qualification"
        ).DEFAULT_ELIMUFLOW_QUALIFICATION,
    )

    for settings in defaults:
        assert settings.scoring.weight_fit == 0.4
        assert settings.scoring.weight_tech == 0.3
        assert settings.scoring.weight_intent_access == 0.3
        assert settings.tiering.tier_1_threshold > settings.tiering.tier_2_threshold


def test_root_shared_contracts_are_reexported_consistently():
    from core.contracts import BaseEnricher, BaseQualifier
    from enrichment.base import BaseEnricher as EnrichmentBase
    from qualification.base import BaseQualifier as QualificationBase

    assert BaseEnricher is EnrichmentBase
    assert BaseQualifier is QualificationBase
