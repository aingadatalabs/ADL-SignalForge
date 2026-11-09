"""ADL ideal-customer-profile qualification defaults."""

from agencies.adl.qualification_engine.rules import QualificationConfig


class ADLICP(QualificationConfig):
    """ADL ICP configuration, initialized with the shared baseline criteria."""


DEFAULT_ADL_ICP = ADLICP()

__all__ = ["ADLICP", "DEFAULT_ADL_ICP"]
