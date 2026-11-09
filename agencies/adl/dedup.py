"""Public entity deduplication interface for the ADL agency pipeline."""

from agencies.adl.identity.entity_resolution import EntityResolver

ADLDeduplicator = EntityResolver

__all__ = ["ADLDeduplicator", "EntityResolver"]
