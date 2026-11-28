from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote


DEFAULT_VERTICAL_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        # High-signal verticals with immediate outbound/GTM infrastructure pain
        "Sales": 30,
        "Marketing": 30,
        "Developer Tools": 25,
        "Analytics": 25,
        "SaaS": 20,
        "Fintech": 20,
        # Broad umbrella tags (useful context, low standalone differentiation)
        "B2B": 10,
        "AI": 10,
    }
)

DEFAULT_BUYER_ROLE_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        # Tier 1: Direct GTM / Pipeline Owners (highest intent for ADL systems)
        "head of growth": 100,
        "head of gtm": 100,
        "vp sales": 100,
        "vp of sales": 100,
        "cro": 100,
        "chief revenue officer": 100,
        # Tier 2: Ultimate Decision Makers (YC early-stage buyer persona)
        "founder": 85,
        "co-founder": 85,
        "ceo": 85,
        "chief executive officer": 85,
        # Tier 3: Functional GTM / Revenue Leads
        "gtm": 50,
        "growth": 50,
        "revenue": 45,
        # Negative modifiers to suppress non-buyer ICs
        "sdr": -60,
        "bdr": -60,
        "intern": -80,
        "assistant": -60,
    }
)

DEFAULT_GTM_HIRING_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        # Tier 1: Scaling outbound & revenue infrastructure right now
        "founding ae": 40,
        "founding account executive": 40,
        "head of growth": 40,
        "revops": 35,
        "revenue operations": 35,
        "growth engineer": 35,
        "gtm engineer": 35,
        # Tier 2: Active pipeline-building roles
        "account executive": 30,
        "sdr": 25,
        "bdr": 25,
        "sales development": 25,
        "business development": 20,
        "demand gen": 25,
        "demand generation": 25,
        "growth": 20,
        "sales engineer": 20,
    }
)

DEFAULT_EXCLUDED_VERTICALS: tuple[str, ...] = (
    "Consumer",
    "B2C",
    "Hardware",
    "Biotech",
    "Therapeutics",
    "Medical Devices",
    "Climate",
    "Government",
    "Nonprofit",
)

DEFAULT_EXCLUDED_KEYWORDS: tuple[str, ...] = (
    "agency",
    "consultancy",
    "consulting firm",
    "non-profit",
    "nonprofit",
    "clinical trial",
    "brick and mortar",
)


def _env_str(*names: str) -> str | None:
    """Return stripped non-empty value for the first matching env var, or None if unset/blank."""
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        cleaned = raw.strip()
        if cleaned:
            return cleaned
    return None


def _env_float(default: float, *names: str) -> float:
    raw = _env_str(*names)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        matched = next((n for n in names if (os.getenv(n) or "").strip()), names[0])
        raise ValueError(f"Invalid float for environment variable {matched}={raw!r}") from exc


def _env_int(default: int, *names: str) -> int:
    raw = _env_str(*names)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        matched = next((n for n in names if (os.getenv(n) or "").strip()), names[0])
        raise ValueError(f"Invalid integer for environment variable {matched}={raw!r}") from exc


def _default_db_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "storage",
        "yc26_leads.db",
    )


def _normalize_proxy_entry(raw_entry: str) -> str | None:
    """Normalize either a standard proxy URL or Webshare `ip:port:user:pass` format."""
    entry = raw_entry.strip()
    if not entry or entry.startswith("#"):
        return None

    if "://" in entry:
        return entry

    parts = entry.split(":")
    if len(parts) == 4:
        host, port, user, password = (p.strip() for p in parts)
        if host and port and user and password:
            safe_user = quote(user, safe="")
            safe_pass = quote(password, safe="")
            return f"http://{safe_user}:{safe_pass}@{host}:{port}"

    return None


def _load_proxy_pool(single_proxy: str | None) -> tuple[str, ...]:
    """Load deduplicated proxy URLs from YC_PROXY_LIST, YC_PROXY_FILE, and single proxy fallback."""
    proxies: list[str] = []

    def _add(val: str | None) -> None:
        normalized = _normalize_proxy_entry(val) if val else None
        if normalized and normalized not in proxies:
            proxies.append(normalized)

    raw_list = _env_str("YC_PROXY_LIST", "SCRAPER_PROXY_LIST")
    if raw_list:
        for item in raw_list.split(","):
            _add(item)

    proxy_file = _env_str("YC_PROXY_FILE", "SCRAPER_PROXY_FILE")
    if proxy_file:
        path = Path(proxy_file)
        if not path.is_absolute():
            repo_candidate = Path.cwd() / path
            module_candidate = Path(__file__).resolve().parent / path
            path = repo_candidate if repo_candidate.exists() else module_candidate
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                _add(line)

    _add(single_proxy)
    return tuple(proxies)


def _compile_weighted_patterns(
    weights: Mapping[str, int],
) -> tuple[tuple[re.Pattern[str], int], ...]:
    """Precompile case-insensitive word-boundary regexes sorted longest-phrase first."""
    sorted_terms = sorted(weights.items(), key=lambda item: len(item[0]), reverse=True)
    return tuple(
        (re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE), score)
        for term, score in sorted_terms
    )


def _compile_term_patterns(terms: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Precompile case-insensitive word-boundary regexes for exclusion matching."""
    sorted_terms = sorted(set(terms), key=len, reverse=True)
    return tuple(
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for term in sorted_terms
        if term.strip()
    )


@dataclass(frozen=True)
class ScraperConfig:
    target_batches: tuple[str, ...] = ("W26", "S26", "F26")
    required_status: str = "Active"
    prioritize_hiring_first: bool = True
    hiring_signal_bonus: int = 25

    # Weighted ICP, Persona & GTM Hiring Signal Maps
    vertical_weights: Mapping[str, int] = field(default_factory=lambda: DEFAULT_VERTICAL_WEIGHTS)
    buyer_role_weights: Mapping[str, int] = field(default_factory=lambda: DEFAULT_BUYER_ROLE_WEIGHTS)
    gtm_hiring_weights: Mapping[str, int] = field(default_factory=lambda: DEFAULT_GTM_HIRING_WEIGHTS)

    # Outreach Exclusions & Language Gate
    excluded_verticals: tuple[str, ...] = DEFAULT_EXCLUDED_VERTICALS
    excluded_keywords: tuple[str, ...] = DEFAULT_EXCLUDED_KEYWORDS
    allowed_languages: tuple[str, ...] = ("en",)

    # Minimum thresholds for qualification & outreach routing
    min_vertical_score: int = 20
    min_buyer_role_score: int = 50

    # Stealth & proxy hygiene (Webshare / ISP proxy pool + jitter/retries)
    proxy_url: str | None = field(
        default_factory=lambda: _env_str("NODEMAVEN_ISP_PROXY", "HTTPS_PROXY")
    )
    proxy_urls: tuple[str, ...] = field(init=False, repr=False, compare=False)
    min_delay_sec: float = field(
        default_factory=lambda: _env_float(2.0, "SCRAPER_MIN_DELAY_SEC", "YC_MIN_DELAY_SEC")
    )
    max_delay_sec: float = field(
        default_factory=lambda: _env_float(3.2, "SCRAPER_MAX_DELAY_SEC", "YC_MAX_DELAY_SEC")
    )
    request_timeout_sec: float = field(
        default_factory=lambda: _env_float(25.0, "SCRAPER_TIMEOUT_SEC", "YC_TIMEOUT_SEC")
    )
    max_retries: int = field(
        default_factory=lambda: _env_int(3, "SCRAPER_MAX_RETRIES", "YC_MAX_RETRIES")
    )

    # Paths
    db_path: str = field(
        default_factory=lambda: _env_str("ADL_DB_PATH", "YC_DB_PATH") or _default_db_path()
    )

    # Optional Algolia override for direct JSON search
    algolia_app_id: str | None = field(default_factory=lambda: _env_str("YC_ALGOLIA_APP_ID"))
    algolia_api_key: str | None = field(default_factory=lambda: _env_str("YC_ALGOLIA_API_KEY"))
    algolia_index_name: str = field(
        default_factory=lambda: _env_str("YC_ALGOLIA_INDEX") or "YCCompany_production"
    )

    # Precompiled regex caches (excluded from repr/compare/init)
    _vertical_patterns: tuple[tuple[re.Pattern[str], int], ...] = field(
        init=False, repr=False, compare=False
    )
    _buyer_patterns: tuple[tuple[re.Pattern[str], int], ...] = field(
        init=False, repr=False, compare=False
    )
    _gtm_hiring_patterns: tuple[tuple[re.Pattern[str], int], ...] = field(
        init=False, repr=False, compare=False
    )
    _excluded_vertical_patterns: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False
    )
    _excluded_keyword_patterns: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        normalized_batches = tuple(
            batch.strip().upper() for batch in self.target_batches if batch and batch.strip()
        )
        if not normalized_batches:
            raise ValueError("target_batches must contain at least one non-empty batch code")

        if not self.required_status or not self.required_status.strip():
            raise ValueError("required_status must be a non-empty string")

        if not self.db_path or not self.db_path.strip():
            raise ValueError("db_path must be a non-empty string")

        normalized_langs = tuple(
            lang.strip().lower() for lang in self.allowed_languages if lang and lang.strip()
        )
        if not normalized_langs:
            raise ValueError("allowed_languages must contain at least one language code")

        if not isinstance(self.vertical_weights, Mapping) or not self.vertical_weights:
            raise ValueError("vertical_weights must be a non-empty mapping")
        if not isinstance(self.buyer_role_weights, Mapping) or not self.buyer_role_weights:
            raise ValueError("buyer_role_weights must be a non-empty mapping")
        if not isinstance(self.gtm_hiring_weights, Mapping) or not self.gtm_hiring_weights:
            raise ValueError("gtm_hiring_weights must be a non-empty mapping")

        if self.min_delay_sec < 0:
            raise ValueError(f"min_delay_sec must be >= 0, got {self.min_delay_sec}")
        if self.max_delay_sec < self.min_delay_sec:
            raise ValueError(
                f"max_delay_sec ({self.max_delay_sec}) must be >= min_delay_sec ({self.min_delay_sec})"
            )
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.request_timeout_sec <= 0:
            raise ValueError(f"request_timeout_sec must be > 0, got {self.request_timeout_sec}")
        if self.hiring_signal_bonus < 0:
            raise ValueError(f"hiring_signal_bonus must be >= 0, got {self.hiring_signal_bonus}")
        if self.min_vertical_score < 0:
            raise ValueError(f"min_vertical_score must be >= 0, got {self.min_vertical_score}")
        if not self.algolia_index_name or not self.algolia_index_name.strip():
            raise ValueError("algolia_index_name must be a non-empty string")

        pool = _load_proxy_pool(self.proxy_url)
        resolved_single_proxy = pool[0] if pool else None

        object.__setattr__(self, "target_batches", normalized_batches)
        object.__setattr__(self, "required_status", self.required_status.strip())
        object.__setattr__(self, "db_path", self.db_path.strip())
        object.__setattr__(self, "allowed_languages", normalized_langs)
        object.__setattr__(self, "proxy_url", resolved_single_proxy)
        object.__setattr__(self, "proxy_urls", pool)
        object.__setattr__(self, "algolia_index_name", self.algolia_index_name.strip())
        object.__setattr__(
            self, "excluded_verticals", tuple(v.strip() for v in self.excluded_verticals if v.strip())
        )
        object.__setattr__(
            self, "excluded_keywords", tuple(k.strip() for k in self.excluded_keywords if k.strip())
        )
        object.__setattr__(self, "vertical_weights", MappingProxyType(dict(self.vertical_weights)))
        object.__setattr__(self, "buyer_role_weights", MappingProxyType(dict(self.buyer_role_weights)))
        object.__setattr__(self, "gtm_hiring_weights", MappingProxyType(dict(self.gtm_hiring_weights)))

        object.__setattr__(self, "_vertical_patterns", _compile_weighted_patterns(self.vertical_weights))
        object.__setattr__(self, "_buyer_patterns", _compile_weighted_patterns(self.buyer_role_weights))
        object.__setattr__(self, "_gtm_hiring_patterns", _compile_weighted_patterns(self.gtm_hiring_weights))
        object.__setattr__(
            self, "_excluded_vertical_patterns", _compile_term_patterns(self.excluded_verticals)
        )
        object.__setattr__(
            self, "_excluded_keyword_patterns", _compile_term_patterns(self.excluded_keywords)
        )

    @property
    def target_verticals(self) -> tuple[str, ...]:
        """Tuple view of configured ICP verticals."""
        return tuple(self.vertical_weights.keys())

    @property
    def target_buyer_roles(self) -> tuple[str, ...]:
        """Tuple view of positive buyer role keywords."""
        return tuple(role for role, weight in self.buyer_role_weights.items() if weight > 0)

    @property
    def gtm_hiring_keywords(self) -> tuple[str, ...]:
        """Tuple view of configured GTM hiring signal keywords."""
        return tuple(self.gtm_hiring_weights.keys())

    def is_excluded(
        self,
        *,
        tags: Iterable[str] = (),
        text: str = "",
        language: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Check whether a company should be disqualified before enrichment/outreach.
        Returns (True, reason) if excluded, else (False, None).
        """
        if language is not None and language.strip():
            lang_code = language.strip().lower().split("-")[0]
            if lang_code not in self.allowed_languages:
                return True, f"language:{lang_code}"

        for tag in tags:
            for pattern in self._excluded_vertical_patterns:
                if pattern.search(tag):
                    return True, f"vertical:{tag}"

        if text:
            for pattern in self._excluded_keyword_patterns:
                match = pattern.search(text)
                if match:
                    return True, f"keyword:{match.group(0).lower()}"

        return False, None

    def score_hiring_roles(self, open_roles: Iterable[str]) -> int:
        """Score open job titles for GTM/revenue hiring intent."""
        matched_scores: list[int] = []
        for role in open_roles:
            for pattern, weight in self._gtm_hiring_patterns:
                if pattern.search(role):
                    matched_scores.append(weight)
                    break
        return sum(matched_scores)

    def score_verticals(
        self,
        tags: Iterable[str],
        *,
        is_hiring: bool = False,
        open_roles: Iterable[str] = (),
    ) -> int:
        """Score a company's tags/verticals + general hiring boost + GTM role hiring intent."""
        matched_scores: list[int] = []
        for tag in tags:
            for pattern, weight in self._vertical_patterns:
                if pattern.search(tag):
                    matched_scores.append(weight)
                    break
        total = sum(matched_scores)
        role_score = self.score_hiring_roles(open_roles)
        if (is_hiring or role_score > 0) and self.prioritize_hiring_first:
            total += self.hiring_signal_bonus
        return total + role_score

    def score_buyer_role(self, title: str) -> int:
        """
        Score a contact's job title using word boundaries.
        Takes the highest positive authority match and applies any negative IC/intern penalties.
        """
        if not title:
            return 0
        positive_matches: list[int] = []
        penalty = 0
        for pattern, weight in self._buyer_patterns:
            if pattern.search(title):
                if weight > 0:
                    positive_matches.append(weight)
                else:
                    penalty += weight
        base = max(positive_matches, default=0)
        return max(0, base + penalty)