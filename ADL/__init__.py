"""Backward-compatible package alias for :mod:`agencies.adl`."""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent / "agencies" / "adl")]
