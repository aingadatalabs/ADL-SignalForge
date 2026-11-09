"""FitEat ICP starter configuration using shared baseline criteria."""

from agencies.adl.qualification_engine.rules import QualificationConfig


class FiteatICP(QualificationConfig):
    """FitEat ICP configuration; agency-specific criteria can be added here."""


DEFAULT_FITEAT_ICP = FiteatICP()

__all__ = ["DEFAULT_FITEAT_ICP", "FiteatICP"]
