from agencies.adl.identity.entity_resolution import EntityResolver


def test_blocked_matching_preserves_length_fallback_candidates():
    resolver = EntityResolver(similarity_threshold=0.7)
    candidates = [
        {"agency_name": "abcd", "domain": "example.com"},
        {"agency_name": "abce"},
    ]

    resolved = resolver.resolve(candidates)

    assert len(resolved) == 1
    assert resolved[0]["observation_count"] == 2
    assert "abce" in resolved[0]["aliases"]


def test_blocked_matching_preserves_shared_token_candidates():
    resolver = EntityResolver(similarity_threshold=0.7)
    candidates = [
        {"agency_name": "Example Group", "domain": "example.com"},
        {"agency_name": "Example Group LLC"},
    ]

    resolved = resolver.resolve(candidates)

    assert len(resolved) == 1
    assert resolved[0]["observation_count"] == 2
