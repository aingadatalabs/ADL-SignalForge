import asyncio

import httpx
import pytest

from core.client import AsyncHTTPClient, ProxyManager
from core.base import BaseEnricher, BaseQualifier
from agencies.adl.enrichment_engine.orchestrator import EnrichmentOrchestrator
from agencies.adl.qualification_engine.orchestrator import QualificationOrchestrator


def test_proxy_manager_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SIGNALFORGE_PROXY_PROVIDER", raising=False)
    assert ProxyManager().get_proxy() is None


def test_proxy_manager_selects_configured_provider(monkeypatch):
    monkeypatch.setenv("SIGNALFORGE_PROXY_PROVIDER", "oxylabs")
    monkeypatch.setenv("OXYLABS_PROXY_URL", "http://proxy.example:8000")
    assert ProxyManager().get_proxy() == "http://proxy.example:8000"


def test_proxy_manager_rejects_missing_provider_configuration(monkeypatch):
    monkeypatch.setenv("SIGNALFORGE_PROXY_PROVIDER", "nodemaven")
    monkeypatch.delenv("NODEMAVEN_PROXY_URL", raising=False)
    with pytest.raises(RuntimeError, match="NODEMAVEN_PROXY_URL"):
        ProxyManager()


def test_async_http_client_manages_client_lifecycle():
    async def exercise_client():
        async with AsyncHTTPClient() as client:
            assert isinstance(client, httpx.AsyncClient)
            assert not client.is_closed

    asyncio.run(exercise_client())


def test_adl_orchestrators_implement_shared_contracts():
    assert issubclass(EnrichmentOrchestrator, BaseEnricher)
    assert issubclass(QualificationOrchestrator, BaseQualifier)
