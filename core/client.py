"""Shared asynchronous HTTP client and optional proxy selection."""

import os
from urllib.parse import urlsplit

import httpx


class ProxyManager:
    _PROXY_ENV_KEYS = {
        "oxylabs": "OXYLABS_PROXY_URL",
        "nodemaven": "NODEMAVEN_PROXY_URL",
    }

    def __init__(self, provider: str | None = None):
        selected_provider = provider or os.getenv("SIGNALFORGE_PROXY_PROVIDER")
        self.provider = selected_provider.strip().lower() if selected_provider else None
        self._proxy_url: str | None = None

        if self.provider is None:
            return

        env_key = self._PROXY_ENV_KEYS.get(self.provider)
        if env_key is None:
            supported = ", ".join(sorted(self._PROXY_ENV_KEYS))
            raise ValueError(f"Unsupported proxy provider {self.provider!r}; choose {supported}.")

        proxy_url = os.getenv(env_key, "").strip()
        if not proxy_url:
            raise RuntimeError(f"{env_key} must be configured for proxy provider {self.provider!r}.")

        parsed = urlsplit(proxy_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{env_key} must be a valid HTTP(S) proxy URL.")

        self._proxy_url = proxy_url

    def get_proxy(self) -> str | None:
        return self._proxy_url


class AsyncHTTPClient:
    def __init__(
        self,
        *,
        proxy_manager: ProxyManager | None = None,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        max_connections: int = 20,
    ):
        proxy = (proxy_manager or ProxyManager()).get_proxy()
        self._client = httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout),
            headers=headers,
            follow_redirects=follow_redirects,
            limits=httpx.Limits(max_connections=max_connections),
        )

    async def __aenter__(self) -> httpx.AsyncClient:
        return await self._client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        await self._client.aclose()
