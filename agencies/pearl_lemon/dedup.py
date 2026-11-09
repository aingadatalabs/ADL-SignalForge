"""Pearl Lemon deduplication entry point using shared entity resolution."""

from agencies.adl.identity.entity_resolution import EntityResolver


class PearlLemonDeduplicator(EntityResolver):
    """Pearl Lemon deduplicator using the shared entity-resolution strategy."""


__all__ = ["PearlLemonDeduplicator"]
