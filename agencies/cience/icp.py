"""CIENCE ICP starter configuration.

The shared baseline is used until CIENCE-specific criteria are supplied.
"""

from agencies.adl.qualification_engine.rules import QualificationConfig


class CIENCEICP(QualificationConfig):
    """CIENCE ICP configuration using shared baseline criteria."""


DEFAULT_CIENCE_ICP = CIENCEICP()

__all__ = ["CIENCEICP", "DEFAULT_CIENCE_ICP"]
