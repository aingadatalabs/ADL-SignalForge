"""Pearl Lemon ICP starter configuration.

The shared baseline is used until Pearl Lemon-specific criteria are supplied.
"""

from agencies.adl.qualification_engine.rules import QualificationConfig


class PearlLemonICP(QualificationConfig):
    """Pearl Lemon ICP configuration using shared baseline criteria."""


DEFAULT_PEARL_LEMON_ICP = PearlLemonICP()

__all__ = ["DEFAULT_PEARL_LEMON_ICP", "PearlLemonICP"]
