"""Stage 1 enrichment provider exports and fetcher pipeline builder."""

from __future__ import annotations

import inspect
import logging
import os
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload, runtime_checkable

from .dns_tech import DnsTechConfig, DnsTechProvider
from .registries import RegistriesConfig, RegistriesProvider
from .search_linkedin import SearchBackendFn, SearchLinkedInConfig, SearchLinkedInProvider
from .web_crawler import CrawlConfig, FrankensurfFetcher, WebCrawlerProvider

if TYPE_CHECKING:
    from ..base import EnrichmentPolicy, EnrichmentResult, LeadRecord
    from ..waterfall import Stage1Fetcher

logger = logging.getLogger(__name__)

DEFAULT_STAGE1_ORDER: tuple[str, ...] = (
    "dns_tech",
    "web_crawler",
    "search_linkedin",
    "registries",
)

PROVIDER_METADATA: dict[str, dict[str, Any]] = {
    "dns_tech": {
        "class_name": "DnsTechProvider",
        "latency_tier": "fast",
        "cost_tier": "free",
        "is_zero_cost": True,
    },
    "web_crawler": {
        "class_name": "WebCrawlerProvider",
        "latency_tier": "medium_slow",
        "cost_tier": "free_or_custom",
        "is_zero_cost": True,
    },
    "search_linkedin": {
        "class_name": "SearchLinkedInProvider",
        "latency_tier": "medium_slow",
        "cost_tier": "free_or_backend",
        "is_zero_cost": True,
    },
    "registries": {
        "class_name": "RegistriesProvider",
        "latency_tier": "slow_or_rate_limited",
        "cost_tier": "keyed_or_free",
        "is_zero_cost": False,
    },
}


@runtime_checkable
class Stage1FetcherProtocol(Protocol):
    """Runtime-checkable callable contract matching Stage1Fetcher."""

    def __call__(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> Awaitable[list[EnrichmentResult] | Iterable[EnrichmentResult | None] | EnrichmentResult | None]:
        ...


@dataclass
class FetcherStatusEntry:
    """Inspection record for a Stage 1 fetcher candidate."""

    name: str
    class_name: str
    status: str  # "enabled", "disabled", "skipped_unconfigured", "skipped_policy"
    latency_tier: str
    cost_tier: str
    is_zero_cost: bool
    reason: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Stage1PipelinePlan:
    """Built Stage 1 fetchers along with inspection metadata for enabled vs. skipped entries."""

    fetchers: list[Stage1Fetcher] = field(default_factory=list)
    entries: list[FetcherStatusEntry] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)

    @property
    def enabled_names(self) -> list[str]:
        return [e.name for e in self.entries if e.status == "enabled"]

    @property
    def skipped_names(self) -> list[str]:
        return [e.name for e in self.entries if e.status != "enabled"]


def _resolve_key(explicit_key: str | None, env_names: tuple[str, ...]) -> str | None:
    """Return a trimmed explicit key if provided, otherwise check fallback env vars."""
    if explicit_key is not None:
        cleaned = explicit_key.strip()
        return cleaned or None
    for env_name in env_names:
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    return None


def _handle_missing_dependency(
    *,
    provider_name: str,
    reason: str,
    strict: bool,
    allow_degraded: bool,
) -> bool:
    """
    Decide whether a provider with missing optional backend/credentials should proceed.

    Returns True to instantiate the provider, False to skip instantiating it.
    Raises ValueError when strict=True.
    """
    message = f"[ADL Stage 1] {provider_name}: {reason}"
    if strict:
        raise ValueError(message)
    logger.warning(message)
    warnings.warn(message, UserWarning, stacklevel=3)
    return allow_degraded


def _apply_config_overrides(cfg: Any, overrides: dict[str, Any]) -> Any:
    """Apply non-None overrides onto a config object if matching fields exist."""
    if cfg is None:
        return None
    valid_overrides = {
        k: v for k, v in overrides.items() if v is not None and hasattr(cfg, k)
    }
    if not valid_overrides:
        return cfg
    try:
        return replace(cfg, **valid_overrides)
    except TypeError:
        for k, v in valid_overrides.items():
            setattr(cfg, k, v)
        return cfg


def _tag_fetcher_metadata(
    fetcher: Any,
    *,
    provider_key: str,
    extra_tags: list[str] | None = None,
) -> Any:
    """Attach declarative latency/cost/order metadata onto a fetcher instance or adapter."""
    meta = dict(PROVIDER_METADATA.get(provider_key, {}))
    tags = [
        f"provider:{provider_key}",
        f"latency:{meta.get('latency_tier', 'unknown')}",
        f"cost:{meta.get('cost_tier', 'unknown')}",
    ]
    if extra_tags:
        tags.extend(extra_tags)
    meta["provider_key"] = provider_key
    meta["tags"] = tags
    try:
        setattr(fetcher, "adl_stage1_meta", meta)
    except Exception:
        pass
    return fetcher


def _coerce_to_stage1_fetcher(instance: Any, provider_key: str) -> Stage1Fetcher:
    """
    Ensure `instance` satisfies the `Stage1Fetcher` callable contract.

    If `instance` is directly callable, return it as-is. Otherwise, adapt a
    compatible async method (`fetch`, `fetch_lead_signals`, `enrich_lead`) into an
    async callable while preserving `adl_stage1_meta` and the underlying provider ref.
    """
    if callable(instance):
        return instance  # type: ignore[return-value]

    for method_name in ("fetch", "fetch_lead_signals", "enrich_lead"):
        candidate_method = getattr(instance, method_name, None)
        if callable(candidate_method):
            async def _adapted_fetcher(
                lead: LeadRecord,
                policy: EnrichmentPolicy,
                _fn: Callable[..., Any] = candidate_method,
            ) -> Any:
                outcome = _fn(lead, policy)
                if inspect.isawaitable(outcome):
                    return await outcome
                return outcome

            existing_meta = getattr(instance, "adl_stage1_meta", None)
            if isinstance(existing_meta, dict):
                setattr(_adapted_fetcher, "adl_stage1_meta", dict(existing_meta))
            setattr(_adapted_fetcher, "provider_instance", instance)
            setattr(_adapted_fetcher, "adapted_method", method_name)
            return _adapted_fetcher  # type: ignore[return-value]

    raise TypeError(
        f"Provider {provider_key!r} ({type(instance).__name__}) is not callable and "
        "exposes no compatible async fetch method ('fetch', 'fetch_lead_signals', 'enrich_lead')."
    )


def _instantiate_provider(
    provider_cls: type[Any],
    *,
    provider_key: str,
    config: Any = None,
    overrides: dict[str, Any] | None = None,
    extra_tags: list[str] | None = None,
) -> Stage1Fetcher:
    """
    Instantiate a provider while tolerating signature evolution between direct kwargs
    and `*Config` fields, tag metadata, and coerce to `Stage1Fetcher`.
    """
    active_overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    effective_config = _apply_config_overrides(config, active_overrides)

    sig = inspect.signature(provider_cls.__init__)
    params = sig.parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    kwargs: dict[str, Any] = {}
    if "config" in params or accepts_var_kw:
        if effective_config is not None:
            kwargs["config"] = effective_config

    for key, val in active_overrides.items():
        if key in params or accepts_var_kw:
            kwargs[key] = val

    raw_instance = provider_cls(**kwargs)
    _tag_fetcher_metadata(raw_instance, provider_key=provider_key, extra_tags=extra_tags)
    return _coerce_to_stage1_fetcher(raw_instance, provider_key)


def _resolve_execution_order(fetcher_order: list[str] | tuple[str, ...] | None) -> list[str]:
    """Resolve declarative Stage 1 execution order, appending any omitted known providers."""
    if not fetcher_order:
        return list(DEFAULT_STAGE1_ORDER)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in fetcher_order:
        key = raw.strip().lower()
        if key not in PROVIDER_METADATA:
            raise ValueError(
                f"Unknown Stage 1 provider {raw!r}; expected one of {sorted(PROVIDER_METADATA.keys())}"
            )
        if key not in seen:
            seen.add(key)
            normalized.append(key)

    for default_key in DEFAULT_STAGE1_ORDER:
        if default_key not in seen:
            seen.add(default_key)
            normalized.append(default_key)

    return normalized


@overload
def build_stage1_fetchers(
    *,
    enable_web_crawler: bool = ...,
    enable_dns_tech: bool = ...,
    enable_search_linkedin: bool = ...,
    enable_registries: bool = ...,
    include_keyed_registries: bool = ...,
    allow_metered_stage1: bool = ...,
    fetcher_order: list[str] | tuple[str, ...] | None = ...,
    crawl_config: CrawlConfig | None = ...,
    frankensurf_fetch: FrankensurfFetcher | None = ...,
    dns_tech_config: DnsTechConfig | None = ...,
    search_linkedin_config: SearchLinkedInConfig | None = ...,
    search_backend: SearchBackendFn | None = ...,
    registries_config: RegistriesConfig | None = ...,
    companies_house_api_key: str | None = ...,
    opencorporates_api_key: str | None = ...,
    strict_validation: bool = ...,
    skip_unconfigured_providers: bool = ...,
    return_plan: Literal[True],
) -> Stage1PipelinePlan: ...


@overload
def build_stage1_fetchers(
    *,
    enable_web_crawler: bool = ...,
    enable_dns_tech: bool = ...,
    enable_search_linkedin: bool = ...,
    enable_registries: bool = ...,
    include_keyed_registries: bool = ...,
    allow_metered_stage1: bool = ...,
    fetcher_order: list[str] | tuple[str, ...] | None = ...,
    crawl_config: CrawlConfig | None = ...,
    frankensurf_fetch: FrankensurfFetcher | None = ...,
    dns_tech_config: DnsTechConfig | None = ...,
    search_linkedin_config: SearchLinkedInConfig | None = ...,
    search_backend: SearchBackendFn | None = ...,
    registries_config: RegistriesConfig | None = ...,
    companies_house_api_key: str | None = ...,
    opencorporates_api_key: str | None = ...,
    strict_validation: bool = ...,
    skip_unconfigured_providers: bool = ...,
    return_plan: Literal[False] = ...,
) -> list[Stage1Fetcher]: ...


def build_stage1_fetchers(
    *,
    enable_web_crawler: bool = True,
    enable_dns_tech: bool = True,
    enable_search_linkedin: bool = True,
    enable_registries: bool = True,
    include_keyed_registries: bool = True,
    allow_metered_stage1: bool = True,
    fetcher_order: list[str] | tuple[str, ...] | None = None,
    crawl_config: CrawlConfig | None = None,
    frankensurf_fetch: FrankensurfFetcher | None = None,
    dns_tech_config: DnsTechConfig | None = None,
    search_linkedin_config: SearchLinkedInConfig | None = None,
    search_backend: SearchBackendFn | None = None,
    registries_config: RegistriesConfig | None = None,
    companies_house_api_key: str | None = None,
    opencorporates_api_key: str | None = None,
    strict_validation: bool = False,
    skip_unconfigured_providers: bool = False,
    return_plan: bool = False,
) -> list[Stage1Fetcher] | Stage1PipelinePlan:
    """
    Instantiate enabled Stage 1 fetchers in configurable/latency-aware order while
    separating zero-cost/local fetchers from keyed/rate-limited registry APIs,
    attaching metadata tags, guaranteeing `Stage1Fetcher` callable compatibility,
    and recording enabled vs. skipped inspection entries.
    """
    order = _resolve_execution_order(fetcher_order)
    fetchers: list[Stage1Fetcher] = []
    entries: list[FetcherStatusEntry] = []

    def _make_entry(
        provider_key: str,
        status: str,
        reason: str | None = None,
        extra_tags: list[str] | None = None,
    ) -> FetcherStatusEntry:
        meta = PROVIDER_METADATA[provider_key]
        tags = [
            f"provider:{provider_key}",
            f"latency:{meta['latency_tier']}",
            f"cost:{meta['cost_tier']}",
            f"status:{status}",
        ]
        if extra_tags:
            tags.extend(extra_tags)
        return FetcherStatusEntry(
            name=provider_key,
            class_name=meta["class_name"],
            status=status,
            latency_tier=meta["latency_tier"],
            cost_tier=meta["cost_tier"],
            is_zero_cost=bool(meta["is_zero_cost"]),
            reason=reason,
            tags=tags,
        )

    for provider_key in order:
        if provider_key == "dns_tech":
            if not enable_dns_tech:
                entries.append(_make_entry("dns_tech", "disabled", reason="enable_dns_tech=False"))
                continue
            extra_tags = ["stage:stage_1", "fast_path"]
            inst = _instantiate_provider(
                DnsTechProvider,
                provider_key="dns_tech",
                config=dns_tech_config,
                extra_tags=extra_tags,
            )
            fetchers.append(inst)
            entries.append(_make_entry("dns_tech", "enabled", extra_tags=extra_tags))

        elif provider_key == "web_crawler":
            if not enable_web_crawler:
                entries.append(
                    _make_entry("web_crawler", "disabled", reason="enable_web_crawler=False")
                )
                continue
            if frankensurf_fetch is None:
                should_add = _handle_missing_dependency(
                    provider_name="WebCrawlerProvider",
                    reason=(
                        "frankensurf_fetch is None; running with default crawler behavior "
                        "(explicitly pass frankensurf_fetch or set enable_web_crawler=False to silence)."
                    ),
                    strict=strict_validation,
                    allow_degraded=not skip_unconfigured_providers,
                )
            else:
                should_add = True

            if should_add:
                extra_tags = ["stage:stage_1", "web_crawl"]
                inst = _instantiate_provider(
                    WebCrawlerProvider,
                    provider_key="web_crawler",
                    config=crawl_config,
                    overrides={"frankensurf_fetch": frankensurf_fetch},
                    extra_tags=extra_tags,
                )
                fetchers.append(inst)
                entries.append(
                    _make_entry(
                        "web_crawler",
                        "enabled",
                        reason="degraded_no_frankensurf_fetch" if frankensurf_fetch is None else None,
                        extra_tags=extra_tags,
                    )
                )
            else:
                entries.append(
                    _make_entry(
                        "web_crawler",
                        "skipped_unconfigured",
                        reason="frankensurf_fetch is None and skip_unconfigured_providers=True",
                    )
                )

        elif provider_key == "search_linkedin":
            if not enable_search_linkedin:
                entries.append(
                    _make_entry(
                        "search_linkedin",
                        "disabled",
                        reason="enable_search_linkedin=False",
                    )
                )
                continue
            if search_backend is None:
                should_add = _handle_missing_dependency(
                    provider_name="SearchLinkedInProvider",
                    reason=(
                        "search_backend is None; running without a custom search backend "
                        "(explicitly pass search_backend or set enable_search_linkedin=False to silence)."
                    ),
                    strict=strict_validation,
                    allow_degraded=not skip_unconfigured_providers,
                )
            else:
                should_add = True

            if should_add:
                extra_tags = ["stage:stage_1", "linkedin_search"]
                inst = _instantiate_provider(
                    SearchLinkedInProvider,
                    provider_key="search_linkedin",
                    config=search_linkedin_config,
                    overrides={"search_backend": search_backend},
                    extra_tags=extra_tags,
                )
                fetchers.append(inst)
                entries.append(
                    _make_entry(
                        "search_linkedin",
                        "enabled",
                        reason="degraded_no_search_backend" if search_backend is None else None,
                        extra_tags=extra_tags,
                    )
                )
            else:
                entries.append(
                    _make_entry(
                        "search_linkedin",
                        "skipped_unconfigured",
                        reason="search_backend is None and skip_unconfigured_providers=True",
                    )
                )

        elif provider_key == "registries":
            if not enable_registries:
                entries.append(
                    _make_entry("registries", "disabled", reason="enable_registries=False")
                )
                continue

            resolved_ch_key = _resolve_key(
                companies_house_api_key,
                ("COMPANIES_HOUSE_API_KEY", "companies_house_API_KEY"),
            )
            resolved_oc_key = _resolve_key(
                opencorporates_api_key,
                ("OPENCORPORATES_API_KEY", "opencorporates_API_KEY"),
            )
            has_keyed_registry_creds = bool(resolved_ch_key or resolved_oc_key)
            keyed_stripped = False

            if has_keyed_registry_creds and (not include_keyed_registries or not allow_metered_stage1):
                msg = (
                    "[ADL Stage 1] RegistriesProvider: keyed registry API credentials are present, "
                    "but keyed/metered Stage 1 registry calls are disabled; stripping API keys so "
                    "only unkeyed public registry paths (if supported) are used."
                )
                logger.info(msg)
                resolved_ch_key = None
                resolved_oc_key = None
                has_keyed_registry_creds = False
                keyed_stripped = True

            if not has_keyed_registry_creds:
                should_add = _handle_missing_dependency(
                    provider_name="RegistriesProvider",
                    reason=(
                        "no Companies House or OpenCorporates API key found in arguments or environment; "
                        "only free/public registry paths (if any) will be available."
                    ),
                    strict=strict_validation,
                    allow_degraded=not skip_unconfigured_providers,
                )
            else:
                should_add = True

            if should_add:
                extra_tags = ["stage:stage_1", "registries"]
                extra_tags.append("keyed_registry" if has_keyed_registry_creds else "unkeyed_registry")
                inst = _instantiate_provider(
                    RegistriesProvider,
                    provider_key="registries",
                    config=registries_config,
                    overrides={
                        "companies_house_api_key": resolved_ch_key,
                        "opencorporates_api_key": resolved_oc_key,
                    },
                    extra_tags=extra_tags,
                )
                fetchers.append(inst)
                reason = None
                if keyed_stripped:
                    reason = "keyed_registry_credentials_stripped_by_policy"
                elif not has_keyed_registry_creds:
                    reason = "unkeyed_registry_fallback"
                entries.append(
                    _make_entry(
                        "registries",
                        "enabled",
                        reason=reason,
                        extra_tags=extra_tags,
                    )
                )
            else:
                entries.append(
                    _make_entry(
                        "registries",
                        "skipped_unconfigured",
                        reason="no registry API keys configured and skip_unconfigured_providers=True",
                    )
                )

    if return_plan:
        return Stage1PipelinePlan(
            fetchers=fetchers,
            entries=entries,
            execution_order=order,
        )
    return fetchers


__all__ = [
    "CrawlConfig",
    "DEFAULT_STAGE1_ORDER",
    "DnsTechConfig",
    "DnsTechProvider",
    "FetcherStatusEntry",
    "FrankensurfFetcher",
    "PROVIDER_METADATA",
    "RegistriesConfig",
    "RegistriesProvider",
    "SearchBackendFn",
    "SearchLinkedInConfig",
    "SearchLinkedInProvider",
    "Stage1FetcherProtocol",
    "Stage1PipelinePlan",
    "WebCrawlerProvider",
    "build_stage1_fetchers",
]