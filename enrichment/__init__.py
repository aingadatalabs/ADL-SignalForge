"""Shared enrichment contracts with legacy ADL engine imports."""

import importlib
from pathlib import Path
import sys

__path__ = [
    str(Path(__file__).resolve().parent),
    str(
        Path(__file__).resolve().parent.parent
        / "agencies"
        / "adl"
        / "enrichment_engine"
    )
]

for _module_name in ("crawler", "extractor", "orchestrator", "schemas"):
    sys.modules[f"{__name__}.{_module_name}"] = importlib.import_module(
        f"agencies.adl.enrichment_engine.{_module_name}"
    )
