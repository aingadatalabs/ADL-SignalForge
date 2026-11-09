"""ElimuFlow ICP starter configuration using shared baseline criteria."""

from agencies.adl.qualification_engine.rules import QualificationConfig


class ElimuFlowICP(QualificationConfig):
    """ElimuFlow ICP configuration; agency-specific criteria can be added here."""


DEFAULT_ELIMUFLOW_ICP = ElimuFlowICP()

__all__ = ["DEFAULT_ELIMUFLOW_ICP", "ElimuFlowICP"]
