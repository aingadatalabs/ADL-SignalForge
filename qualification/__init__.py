"""Shared qualification contracts with legacy ADL engine imports."""

import importlib
from pathlib import Path
import sys

_ENGINE_PATH = (
    Path(__file__).resolve().parent.parent
    / "agencies"
    / "adl"
    / "qualification_engine"
)
__path__ = [str(Path(__file__).resolve().parent), str(_ENGINE_PATH)]

for _module_name in ("orchestrator", "rules", "schemas", "scorer", "tierer"):
    sys.modules[f"{__name__}.{_module_name}"] = importlib.import_module(
        f"agencies.adl.qualification_engine.{_module_name}"
    )
