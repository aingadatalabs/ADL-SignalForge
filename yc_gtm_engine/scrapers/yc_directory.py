from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from yc_gtm_engine.config import ScraperConfig
from yc_gtm_engine.db import YCLeadsDatabase


logger = logging.getLogger(__name__)

BASE_URL = "https://www.ycombinator.com"
COMPANIES_DIR_URL = f"{BASE_URL}/companies"
MAX_BACKOFF_SEC = 30.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


class YCDirectoryScraper:
    _NON_COMPANY_SLUGS: frozenset[str] = frozenset(
        {
            "",
            "industry",
            "batch",
            "location",
            "founders",
            "jobs",
            "edit",
            "claim",
            "login",
            "signup",
        }
    )

    _SLUG_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    _COMPANY_HREF_RE = re.compile(
        r"""href\s*=\s*(?:"|')?(?:https?://(?:www\.)?ycombinator\.com)?/companies/([a-z0-9-]+)(?:/jobs)?(?=(?:/jobs)?(?:/|["'?#&\s>]|$))""",
        re.IGNORECASE,
    )

    _EXCLUDED_WEBSITE_HOSTS: frozenset[str] = frozenset(
        {
            "ycombinator.com",
            "linkedin.com",
            "twitter.com",
            "x.com",
            "crunchbase.com",
            "facebook.com",
            "instagram.com",
            "github.com",
            "youtube.com",
            "youtu.be",
            "tiktok.com",
            "medium.com",
            "substack.com",
            "wellfound.com",
            "angel.co",
            "producthunt.com",
        }
    )

    _NON_HOMEPAGE_PATHS: tuple[str, ...] = (
        "/jobs",
        "/careers",
        "/blog",
        "/posts",
        "/news",
        "/docs",
        "/documentation",
        "/privacy",
        "/terms",
        "/legal",
    )

    _STATUS_MAP: dict[str, str] = {
        "active": "Active",
        "inactive": "Inactive",
        "acquired": "Acquired",
        "exited": "Exited",
        "shutdown": "Inactive",
        "shut down": "Inactive",
        "public": "Public",
    }

    _BATCH_PHRASE_MAP: dict[str, str] = {
        "winter 2026": "W26",
        "summer 2026": "S26",
        "fall 2026": "F26",
        "spring 2026": "S26",
        "winter 2025": "W25",
        "summer 2025": "S25",
        "fall 2025": "F25",
    }

    _BATCH_CODE_TO_LABELS: dict[str, tuple[str, ...]] = {
        "W26": ("W26", "Winter 2026", "Winter '26"),
        "S26": ("S26", "Summer 2026", "Summer '26", "Spring 2026"),
        "F26": ("F26", "Fall 2026", "Fall '26"),
        "W25": ("W25", "Winter 2025", "Winter '25"),
        "S25": ("S25", "Summer 2025", "Summer '25", "Spring 2025"),
        "F25": ("F25", "Fall 2025", "Fall '25"),
    }

    def __init__(self, config: ScraperConfig, db: YCLeadsDatabase) -> None:
        self.config = config
        self.db = db
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._proxy_idx = 0
        self._proxy_cooldown_until: dict[str, float] = {}

    @staticmethod
    def _has_word(term: str, text: str) -> bool:
        """Boundary-aware substring check so short tokens like 'ai', 'gtm', or 'sales' never collide."""
        cleaned_term = term.strip()
        if not cleaned_term or not text:
            return False
        pattern = re.compile(rf"(?<!\w){re.escape(cleaned_term)}(?!\w)", re.IGNORECASE)
        return pattern.search(text) is not None

    @classmethod
    def _normalize_slug(cls, raw_slug: str | None) -> str | None:
        if not raw_slug:
            return None
        slug = raw_slug.strip().strip("/\"'").lower()
        if not slug or not cls._SLUG_TOKEN_RE.match(slug) or slug in cls._NON_COMPANY_SLUGS:
            return None
        return slug

    @classmethod
    def _normalize_batch_code(cls, raw_batch: Any) -> str:
        if not raw_batch:
            return "UNKNOWN"
        text = str(raw_batch).strip()
        upper = text.upper()
        if re.fullmatch(r"[WSF]\d{2}", upper):
            return upper
        lower = " ".join(text.lower().split())
        if lower in cls._BATCH_PHRASE_MAP:
            return cls._BATCH_PHRASE_MAP[lower]
        for code, labels in cls._BATCH_CODE_TO_LABELS.items():
            if any(lower == lbl.lower() for lbl in labels):
                return code
        return upper

    def _next_proxy(self) -> str | None:
        pool = self.config.proxy_urls
        if not pool:
            return None

        now = time.monotonic()
        healthy = [p for p in pool if self._proxy_cooldown_until.get(p, 0.0) <= now]
        candidates = healthy if healthy else list(pool)

        proxy = candidates[self._proxy_idx % len(candidates)]
        self._proxy_idx += 1
        return proxy

    def _penalize_proxy(self, proxy: str | None, cooldown_sec: float = 60.0) -> None:
        if proxy:
            self._proxy_cooldown_until[proxy] = time.monotonic() + cooldown_sec

    def _sleep_jitter(self, multiplier: float = 1.0) -> None:
        delay = random.uniform(self.config.min_delay_sec, self.config.max_delay_sec) * multiplier
        time.sleep(delay)

    @staticmethod
    def _parse_retry_after(header_val: str | None) -> float | None:
        if not header_val:
            return None
        try:
            seconds = float(header_val.strip())
            return max(0.0, min(seconds, MAX_BACKOFF_SEC))
        except ValueError:
            return None

    def _backoff_sleep(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(MAX_BACKOFF_SEC, (2 ** (attempt - 1)) * 1.5 + random.uniform(0.25, 1.0))
        time.sleep(delay)

    def _fetch_html(self, url: str) -> str | None:
        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(1, attempts + 1):
            proxy = self._next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            proxy_label = proxy.split("@")[-1] if proxy and "@" in proxy else (proxy or "direct")
            try:
                resp = self.session.get(
                    url,
                    timeout=self.config.request_timeout_sec,
                    proxies=proxies,
                )
                status = resp.status_code

                if status == 200 and resp.text:
                    return resp.text

                # Non-retryable 4xx responses
                if status in (404, 410):
                    logger.debug("HTTP %d (not found) for %s — skipping without retry", status, url)
                    return None
                if status in (401, 403):
                    self._penalize_proxy(proxy, cooldown_sec=120.0)
                    logger.warning(
                        "HTTP %d (blocked/forbidden) for %s via %s — cooling down proxy",
                        status,
                        url,
                        proxy_label,
                    )
                    return None
                if 400 <= status < 500 and status != 429:
                    logger.warning("Non-retryable HTTP %d for %s — skipping", status, url)
                    return None

                # Retryable 429 / 5xx responses
                if status == 429 or status >= 500:
                    self._penalize_proxy(proxy, cooldown_sec=45.0)
                    if attempt >= attempts:
                        logger.warning("HTTP %d persisted for %s after %d attempts", status, url, attempt)
                        return None
                    retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                    logger.info(
                        "HTTP %d on %s via %s (attempt %d/%d) — backing off%s",
                        status,
                        url,
                        proxy_label,
                        attempt,
                        attempts,
                        f" ({retry_after:.1f}s Retry-After)" if retry_after is not None else "",
                    )
                    self._backoff_sleep(attempt, retry_after=retry_after)
                    continue

                logger.warning("Unexpected HTTP %d for %s", status, url)
                return None

            except requests.RequestException as exc:
                self._penalize_proxy(proxy, cooldown_sec=45.0)
                if attempt >= attempts:
                    logger.warning(
                        "Request error on %s via %s after %d attempts: %s",
                        url,
                        proxy_label,
                        attempt,
                        exc,
                    )
                    return None
                logger.info(
                    "Request error on %s via %s (attempt %d/%d): %s — retrying",
                    url,
                    proxy_label,
                    attempt,
                    attempts,
                    exc,
                )
                self._backoff_sleep(attempt)

        return None

    def _discover_slugs_from_algolia(self, max_pages_per_batch: int = 10) -> list[dict[str, Any]]:
        """
        Discover YC company slugs via Algolia search using ScraperConfig credentials.
        Expands target batch codes into both short ('W26') and full ('Winter 2026')
        facet filters so hits match regardless of index formatting, falling back
        to unfiltered pagination if facet filtering returns zero hits.
        """
        app_id = self.config.algolia_app_id
        api_key = self.config.algolia_api_key
        index_name = self.config.algolia_index_name

        if not app_id or not api_key:
            logger.debug("Algolia credentials not configured; skipping Algolia slug discovery")
            return []

        endpoint = f"https://{app_id}-dsn.algolia.net/1/indexes/{index_name}/query"
        headers = {
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": api_key,
            "Content-Type": "application/json",
        }

        discovered: dict[str, dict[str, Any]] = {}
        target_set = set(self.config.target_batches)

        def _ingest_hits(hits: list[dict[str, Any]], expected_batch: str | None = None) -> None:
            for hit in hits:
                slug = self._normalize_slug(hit.get("slug"))
                if not slug:
                    continue
                norm_batch = self._normalize_batch_code(hit.get("batch"))
                if expected_batch and norm_batch != expected_batch:
                    continue
                if target_set and norm_batch not in target_set:
                    continue

                raw_status = str(hit.get("status") or "").strip()
                if (
                    self.config.required_status
                    and raw_status
                    and raw_status.lower() != self.config.required_status.lower()
                ):
                    continue

                is_hiring = bool(hit.get("isHiring", False))
                entry = discovered.setdefault(
                    slug,
                    {
                        "slug": slug,
                        "yc_url": f"{BASE_URL}/companies/{slug}",
                        "batch": norm_batch,
                        "is_hiring": is_hiring,
                    },
                )
                if norm_batch != "UNKNOWN":
                    entry["batch"] = norm_batch
                if is_hiring:
                    entry["is_hiring"] = True
                if hit.get("name") and not entry.get("company_name"):
                    entry["company_name"] = self._clean_brand_name(str(hit["name"]), slug)
                if hit.get("one_liner") and not entry.get("one_liner"):
                    entry["one_liner"] = str(hit["one_liner"]).strip()

        def _query_algolia(payload: dict[str, Any]) -> dict[str, Any] | None:
            attempts = max(1, self.config.max_retries + 1)
            for attempt in range(1, attempts + 1):
                proxy = self._next_proxy()
                proxies = {"http": proxy, "https": proxy} if proxy else None
                try:
                    resp = self.session.post(
                        endpoint,
                        headers=headers,
                        json=payload,
                        proxies=proxies,
                        timeout=self.config.request_timeout_sec,
                    )
                    if resp.status_code == 200:
                        return resp.json()
                    if resp.status_code in (400, 401, 403, 404):
                        logger.warning(
                            "Algolia non-retryable HTTP %d: %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                        return None
                    self._penalize_proxy(proxy, cooldown_sec=45.0)
                    if attempt < attempts:
                        self._backoff_sleep(attempt)
                except requests.RequestException as exc:
                    self._penalize_proxy(proxy, cooldown_sec=45.0)
                    if attempt >= attempts:
                        logger.warning("Algolia request failed after %d attempts: %s", attempt, exc)
                        return None
                    self._backoff_sleep(attempt)
            return None

        # Pass 1: Facet-filtered query per target batch (fast & targeted)
        for batch_code in self.config.target_batches:
            batch_labels = self._BATCH_CODE_TO_LABELS.get(batch_code, (batch_code,))
            batch_or_facets = [f"batch:{label}" for label in batch_labels]

            page = 0
            while page < max_pages_per_batch:
                payload: dict[str, Any] = {
                    "query": "",
                    "page": page,
                    "hitsPerPage": 100,
                    "facetFilters": [batch_or_facets],
                    "attributesToRetrieve": [
                        "slug",
                        "name",
                        "batch",
                        "one_liner",
                        "isHiring",
                        "status",
                    ],
                }
                data = _query_algolia(payload)
                if not data:
                    break

                hits = data.get("hits") or []
                nb_pages = int(data.get("nbPages") or 0)
                _ingest_hits(hits, expected_batch=batch_code)

                page += 1
                if page >= nb_pages or not hits:
                    break
                self._sleep_jitter(multiplier=0.4)

        # Pass 2: Fallback if index does not facet on `batch`
        if not discovered:
            logger.info("Algolia batch facet filter returned 0 hits; trying unfiltered index scan")
            page = 0
            while page < max_pages_per_batch:
                payload = {
                    "query": "",
                    "page": page,
                    "hitsPerPage": 100,
                    "attributesToRetrieve": [
                        "slug",
                        "name",
                        "batch",
                        "one_liner",
                        "isHiring",
                        "status",
                    ],
                }
                data = _query_algolia(payload)
                if not data:
                    break

                hits = data.get("hits") or []
                nb_pages = int(data.get("nbPages") or 0)
                _ingest_hits(hits, expected_batch=None)

                page += 1
                if page >= nb_pages or not hits:
                    break
                self._sleep_jitter(multiplier=0.4)

        logger.info("Algolia discovery found %d matching candidate slugs", len(discovered))
        return list(discovered.values())

    def _discover_slugs_from_directory(self) -> list[dict[str, Any]]:
        """
        Fallback HTML discovery from /companies query URLs and any embedded
        JSON payloads in the HTML response, normalizing sub-paths to root slugs.
        """
        discovered: dict[str, dict[str, Any]] = {}
        batch_queries = [f"{COMPANIES_DIR_URL}?batch={b}" for b in self.config.target_batches]

        for url in batch_queries:
            html = self._fetch_html(url)
            self._sleep_jitter(multiplier=0.75)
            if not html:
                continue

            # 1. Extract /companies/<slug> and /companies/<slug>/jobs links cleanly
            for match in self._COMPANY_HREF_RE.finditer(html):
                full_match = match.group(0).lower()
                slug = self._normalize_slug(match.group(1))
                if not slug:
                    continue

                discovered_via_jobs = "/jobs" in full_match
                entry = discovered.setdefault(
                    slug,
                    {
                        "slug": slug,
                        "yc_url": f"{BASE_URL}/companies/{slug}",
                        "is_hiring": discovered_via_jobs,
                    },
                )
                if discovered_via_jobs:
                    entry["is_hiring"] = True

            # 2. Check for embedded JSON objects containing slug/batch/hiring info
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script"):
                raw = script.string or script.get_text() or ""
                if '"slug"' not in raw or not any(b in raw for b in self.config.target_batches):
                    continue
                for obj_match in re.finditer(r"\{[^{}]{40,1200}\}", raw):
                    snippet = obj_match.group(0)
                    slug_m = re.search(r'"slug"\s*:\s*"([a-z0-9-]+)"', snippet, re.I)
                    if not slug_m:
                        continue
                    slug = self._normalize_slug(slug_m.group(1))
                    if not slug:
                        continue

                    batch_m = re.search(r'"batch"\s*:\s*"([WSF]\d{2})"', snippet, re.I)
                    hiring_m = re.search(r'"isHiring"\s*:\s*(true|false)', snippet, re.I)
                    name_m = re.search(r'"name"\s*:\s*"([^"]{1,120})"', snippet)

                    entry = discovered.setdefault(
                        slug,
                        {
                            "slug": slug,
                            "yc_url": f"{BASE_URL}/companies/{slug}",
                            "is_hiring": False,
                        },
                    )
                    if batch_m:
                        entry["batch"] = batch_m.group(1).upper()
                    if hiring_m and hiring_m.group(1).lower() == "true":
                        entry["is_hiring"] = True
                    if name_m and not entry.get("company_name"):
                        entry["company_name"] = self._clean_brand_name(name_m.group(1), slug)

        return list(discovered.values())

    @staticmethod
    def _parse_team_size(text: str) -> int | None:
        m = re.search(r"(?:team\s*size|employees?)\s*[:\-]?\s*(\d{1,4})", text, re.I)
        if m:
            return int(m.group(1))
        return None

    @classmethod
    def _is_excluded_website_host(cls, host: str) -> bool:
        clean_host = host.lower().split(":")[0].removeprefix("www.")
        if not clean_host or clean_host == "ycombinator.com" or clean_host.endswith(".ycombinator.com"):
            return True
        return any(
            clean_host == ex or clean_host.endswith(f".{ex}")
            for ex in cls._EXCLUDED_WEBSITE_HOSTS
        )

    @classmethod
    def _extract_website_url(cls, soup: BeautifulSoup, slug: str | None = None) -> str | None:
        all_anchors = soup.find_all("a", href=True)
        total_anchors = max(len(all_anchors), 1)
        best_url: str | None = None
        best_score = -10**9
        slug_roots = {
            tok for tok in re.split(r"[-_]+", (slug or "").lower()) if len(tok) >= 3
        }

        for idx, a in enumerate(all_anchors):
            href = a["href"].strip()
            if not href.startswith(("http://", "https://")):
                continue

            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if cls._is_excluded_website_host(host):
                continue

            score = 0
            clean_host = host.split(":")[0].removeprefix("www.")
            host_root = clean_host.split(".")[0] if clean_host else ""
            path = (parsed.path or "/").lower()

            raw_rel = a.get("rel")
            if isinstance(raw_rel, (list, tuple)):
                rel_tokens = {str(tok).strip().lower() for tok in raw_rel if str(tok).strip()}
            elif isinstance(raw_rel, str):
                rel_tokens = {tok.strip().lower() for tok in raw_rel.split() if tok.strip()}
            else:
                rel_tokens = set()

            target = (a.get("target") or "").strip().lower()
            if target == "_blank":
                score += 25
            if {"noopener", "noreferrer"} & rel_tokens:
                score += 20

            label_text = " ".join(
                part.strip().lower()
                for part in (
                    a.get_text(" ", strip=True),
                    str(a.get("aria-label") or ""),
                    str(a.get("title") or ""),
                )
                if part and part.strip()
            )
            if (
                cls._has_word("website", label_text)
                or cls._has_word("homepage", label_text)
                or "visit site" in label_text
            ):
                score += 35
            if clean_host and clean_host in label_text:
                score += 40
            if host_root and any(cls._has_word(token, host_root) for token in slug_roots):
                score += 30

            # Penalize secondary subdomains (docs., blog., status., app., api.)
            if host_root in {"docs", "blog", "status", "help", "support", "app", "api"}:
                score -= 25

            if idx < max(8, total_anchors // 4):
                score += 20
            elif idx > (total_anchors * 3) // 4:
                score -= 20

            if a.find_parent(["header", "nav", "main"]) is not None:
                score += 10
            if a.find_parent("footer") is not None:
                score -= 30

            if path in ("", "/"):
                score += 15
            elif any(path == p or path.startswith(f"{p}/") for p in cls._NON_HOMEPAGE_PATHS):
                score -= 35

            if parsed.query:
                score -= 10

            normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
            if parsed.query:
                normalized_url = f"{normalized_url}?{parsed.query}"

            if score > best_score:
                best_score = score
                best_url = normalized_url

        return best_url

    @staticmethod
    def _extract_embedded_company_json(soup: BeautifulSoup, slug: str) -> dict[str, Any]:
        """
        Extract structured company fields (status, isHiring, batch) from
        embedded JSON payloads scoped specifically to `slug`.
        """
        escaped_slug = re.escape(slug)
        for script in soup.find_all("script"):
            raw = script.string or script.get_text() or ""
            if slug not in raw:
                continue
            for obj_match in re.finditer(r"\{[^{}]{30,2500}\}", raw):
                snippet = obj_match.group(0)
                if not re.search(rf'"slug"\s*:\s*"{escaped_slug}"', snippet, re.I):
                    continue
                extracted: dict[str, Any] = {}
                status_m = re.search(
                    r'"status"\s*:\s*"(Active|Inactive|Acquired|Exited|Public)"',
                    snippet,
                    re.I,
                )
                if status_m:
                    extracted["status"] = status_m.group(1).capitalize()

                hiring_m = re.search(r'"isHiring"\s*:\s*(true|false)', snippet, re.I)
                if hiring_m:
                    extracted["is_hiring"] = hiring_m.group(1).lower() == "true"

                batch_m = re.search(r'"batch"\s*:\s*"([WSF]\d{2})"', snippet, re.I)
                if batch_m:
                    extracted["batch"] = batch_m.group(1).upper()

                if extracted:
                    return extracted
        return {}

    @classmethod
    def _extract_batch(
        cls,
        page_text: str,
        seed_meta: dict[str, Any],
        embedded_meta: dict[str, Any],
    ) -> str:
        raw = (seed_meta.get("batch") or embedded_meta.get("batch") or "").strip()
        if raw:
            normalized = cls._normalize_batch_code(raw)
            if normalized != "UNKNOWN":
                return normalized

        code_match = re.search(r"\b([WSF]\d{2})\b", page_text)
        if code_match:
            return code_match.group(1).upper()

        lower_text = " ".join(page_text.lower().split())
        for phrase, code in cls._BATCH_PHRASE_MAP.items():
            if phrase in lower_text:
                return code

        return "UNKNOWN"

    @staticmethod
    def _clean_brand_name(raw_name: str, slug: str) -> str:
        cleaned = " ".join(raw_name.split()).strip()
        if ":" in cleaned:
            left, _ = cleaned.split(":", maxsplit=1)
            left_clean = left.strip()
            slug_tokens = set(re.sub(r"[^a-z0-9]+", " ", slug.lower()).split())
            left_tokens = set(re.sub(r"[^a-z0-9]+", " ", left_clean.lower()).split())
            if 1 <= len(left_clean) <= 45 and (slug_tokens & left_tokens):
                return left_clean
        return cleaned

    @classmethod
    def _extract_company_name(cls, soup: BeautifulSoup, slug: str, seed_meta: dict[str, Any]) -> str:
        if seed_meta.get("company_name"):
            return cls._clean_brand_name(str(seed_meta["company_name"]), slug)

        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            cleaned = re.split(
                r"\s*[|\-–—]\s*Y Combinator",
                str(og_title["content"]),
                maxsplit=1,
            )[0].strip()
            if cleaned and len(cleaned) <= 100:
                return cls._clean_brand_name(cleaned, slug)

        if soup.title and soup.title.string:
            cleaned = re.split(
                r"\s*[|\-–—]\s*Y Combinator",
                soup.title.string,
                maxsplit=1,
            )[0].strip()
            if cleaned and len(cleaned) <= 100:
                return cls._clean_brand_name(cleaned, slug)

        h1 = soup.find("h1")
        if h1:
            h1_text = " ".join(h1.get_text(" ", strip=True).split())
            if (
                h1_text
                and len(h1_text) <= 80
                and not h1_text.lower().startswith(("what is ", "full demo"))
            ):
                return cls._clean_brand_name(h1_text, slug)

        return slug.replace("-", " ").title()

    @classmethod
    def _extract_status(cls, soup: BeautifulSoup, embedded_meta: dict[str, Any]) -> str:
        """
        Determine company status from structured JSON or compact header/main badges,
        avoiding false positives from one-liners or footers.
        """
        if embedded_meta.get("status"):
            return str(embedded_meta["status"])

        scope = soup.find("main") or soup.body or soup
        found_statuses: set[str] = set()

        for node in scope.find_all(["span", "div", "li", "dd", "p"], limit=140):
            if node.find_parent("footer") is not None:
                continue
            text = " ".join(node.get_text(" ", strip=True).split())
            if not text or len(text) > 28:
                continue

            lowered = text.lower()
            if "no longer active" in lowered or "not active" in lowered:
                return "Inactive"

            cleaned = re.sub(r"^status\s*:\s*", "", lowered).strip()
            if cleaned in cls._STATUS_MAP:
                found_statuses.add(cls._STATUS_MAP[cleaned])

        for terminal in ("Acquired", "Exited", "Inactive", "Public"):
            if terminal in found_statuses:
                return terminal
        if "Active" in found_statuses:
            return "Active"

        return "Active"

    @staticmethod
    def _extract_is_hiring(
        soup: BeautifulSoup,
        slug: str,
        seed_meta: dict[str, Any],
        embedded_meta: dict[str, Any],
    ) -> bool:
        """
        Determine active hiring strictly from structured metadata, slug-scoped job links,
        or compact hiring badges inside the company profile scope.
        """
        if "is_hiring" in embedded_meta:
            return bool(embedded_meta["is_hiring"])

        scope = soup.find("main") or soup.body or soup
        escaped_slug = re.escape(slug)

        jobs_href_re = re.compile(
            rf"^(?:https?://(?:www\.)?ycombinator\.com)?/companies/{escaped_slug}/jobs(?:[/?#]|$)",
            re.IGNORECASE,
        )
        for a in scope.find_all("a", href=jobs_href_re):
            if a.find_parent(["nav", "footer"]) is not None:
                continue
            if a.get("aria-disabled", "").lower() == "true" or "disabled" in (a.get("class") or []):
                continue
            link_text = " ".join(a.get_text(" ", strip=True).split()).lower()
            if re.search(r"\b(?:0|no)\s+(?:open\s+)?(?:jobs|roles|positions)\b", link_text):
                continue
            if "not hiring" in link_text:
                return False
            return True

        for node in scope.find_all(["span", "div", "li", "p", "a"], limit=140):
            if node.find_parent(["nav", "footer"]) is not None:
                continue
            text = " ".join(node.get_text(" ", strip=True).split())
            if not text or len(text) > 32:
                continue
            lowered = text.lower()
            if re.search(r"\b(?:not\s+hiring|no\s+open\s+roles|0\s+jobs)\b", lowered):
                return False
            if re.fullmatch(
                r"(?:actively\s+hiring|hiring|we(?:'re|\s+are)\s+hiring|\d+\s+open\s+(?:roles|jobs))",
                lowered,
            ):
                if not lowered.startswith("0 "):
                    return True

        return bool(seed_meta.get("is_hiring", False))

    @staticmethod
    def _normalize_linkedin_profile_url(href: str) -> str | None:
        parsed = urlparse(href.strip())
        if "linkedin.com" not in parsed.netloc.lower() or "/in/" not in parsed.path.lower():
            return None
        clean_path = parsed.path.rstrip("/")
        return f"https://www.linkedin.com{clean_path}"

    @classmethod
    def _find_founder_card(cls, link: Any) -> Any:
        """
        Walk up the DOM from a founder's LinkedIn anchor and stop at the tightest
        container representing a single founder so adjacent founder cards never bleed.
        """
        best_container = link.parent or link
        prev_unique_profiles = 1

        for depth, parent in enumerate(link.find_parents(["div", "section", "article", "li"])):
            if depth >= 6 or parent.name in ("body", "html", "main", "ul", "ol"):
                break

            parent_text = " ".join(parent.get_text(" ", strip=True).split())
            if len(parent_text) > 550:
                break

            unique_profiles = {
                norm
                for a in parent.find_all("a", href=True)
                if (norm := cls._normalize_linkedin_profile_url(a["href"])) is not None
            }
            if len(unique_profiles) > 1 or len(unique_profiles) > prev_unique_profiles:
                break

            # Stop if this container starts repeating role/header blocks for multiple people
            role_matches = re.findall(
                r"(?<!\w)(?:founder|co-founder|ceo|cto|coo|cpo|cmo|cro)(?!\w)",
                parent_text,
                re.IGNORECASE,
            )
            if len(role_matches) > 1:
                break

            prev_unique_profiles = max(prev_unique_profiles, len(unique_profiles))
            best_container = parent

        return best_container

    def _is_priority_buyer(self, role: str | None) -> bool:
        """Delegate buyer-role authority scoring using boundary-aware role checks."""
        if not role:
            return False
        if not any(self._has_word(kw, role) for kw in self.config.target_buyer_roles):
            return False
        return self.config.score_buyer_role(role) >= self.config.min_buyer_role_score

    def _match_vertical_names(self, text: str) -> list[str]:
        """Match configured vertical labels against `text` using word-boundary regexes."""
        if not text:
            return []
        matched: list[str] = []
        seen_lower: set[str] = set()
        for pattern, _ in self.config._vertical_patterns:
            m = pattern.search(text)
            if not m:
                continue
            raw_match = m.group(0).strip()
            if not self._has_word(raw_match, text):
                continue
            key = raw_match.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            canonical = next(
                (v for v in self.config.target_verticals if v.lower() == key),
                raw_match,
            )
            matched.append(canonical)
        return matched

    def _score_icp(
        self,
        batch: str,
        verticals: list[str],
        team_size: int | None,
        is_hiring: bool,
        one_liner: str,
    ) -> tuple[int, str]:
        excluded, _ = self.config.is_excluded(tags=verticals, text=one_liner)
        if excluded:
            return 0, "TIER_EXCLUDED"

        score = 0
        if batch in self.config.target_batches:
            score += 25

        bounded_verticals = [
            v for v in verticals if self._has_word(v, " ".join(verticals)) or self._has_word(v, one_liner)
        ]
        score += self.config.score_verticals(bounded_verticals, is_hiring=is_hiring)

        matched_lower = {v.lower() for v in bounded_verticals}
        if one_liner:
            for pattern, weight in self.config._vertical_patterns:
                m = pattern.search(one_liner)
                if m and self._has_word(m.group(0), one_liner) and m.group(0).lower() not in matched_lower:
                    score += weight
                    matched_lower.add(m.group(0).lower())

        if team_size is not None and 1 <= team_size <= 25:
            score += 15
        elif team_size is None:
            score += 5

        if score >= 85:
            tier = "TIER_1_HOT"
        elif score >= 55:
            tier = "TIER_2_WARM"
        else:
            tier = "TIER_3_MONITOR"
        return score, tier

    def _extract_founders_from_soup(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        founders: list[dict[str, Any]] = []
        seen_profiles: set[str] = set()
        seen_names: set[str] = set()

        scope = soup.find("main") or soup.body or soup
        for link in scope.find_all("a", href=True):
            linkedin_url = self._normalize_linkedin_profile_url(link["href"])
            if not linkedin_url or linkedin_url in seen_profiles:
                continue
            seen_profiles.add(linkedin_url)

            container = self._find_founder_card(link)
            text_lines = [
                line.strip()
                for line in container.get_text("\n", strip=True).splitlines()
                if line.strip()
            ]

            founder_name = ""
            role = ""
            bio_parts: list[str] = []
            twitter_url = None

            for tw in container.find_all("a", href=True):
                tw_href = tw["href"].strip()
                if ("twitter.com/" in tw_href or "x.com/" in tw_href) and "/status/" not in tw_href:
                    twitter_url = tw_href.split("?")[0]
                    break

            link_text = link.get_text(" ", strip=True)
            if link_text and len(link_text) <= 60 and "linkedin" not in link_text.lower():
                founder_name = link_text

            for line in text_lines:
                if line in ("LinkedIn", "Twitter", "X", "Bio", "Website"):
                    continue
                if not founder_name and 2 <= len(line) <= 60:
                    founder_name = line
                    continue
                if founder_name and line == founder_name:
                    continue
                if not role and len(line) <= 80 and (
                    self.config.score_buyer_role(line) > 0
                    or re.search(
                        r"(?<!\w)(?:founder|co-founder|ceo|cto|coo|cpo|cmo|cro|president|partner|head of|vp)(?!\w)",
                        line,
                        re.IGNORECASE,
                    )
                ):
                    role = line
                    continue
                bio_parts.append(line)

            if not founder_name:
                slug_part = urlparse(linkedin_url).path.strip("/").split("/")[-1]
                founder_name = slug_part.replace("-", " ").title()

            norm_name = founder_name.lower()
            if norm_name and norm_name not in seen_names:
                seen_names.add(norm_name)
                resolved_role = role or "Founder"
                founders.append(
                    {
                        "founder_name": founder_name,
                        "role": resolved_role,
                        "bio": " ".join(bio_parts[:3]) if bio_parts else None,
                        "linkedin_url": linkedin_url,
                        "twitter_url": twitter_url,
                        "is_priority_buyer": self._is_priority_buyer(resolved_role),
                    }
                )

        return founders

    def parse_company_page(self, slug: str, seed_meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
        clean_slug = self._normalize_slug(slug)
        if not clean_slug:
            logger.debug("Skipping invalid or non-company slug=%r", slug)
            return None

        seed_meta = seed_meta or {}
        yc_url = f"{BASE_URL}/companies/{clean_slug}"
        html = self._fetch_html(yc_url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text("\n", strip=True)
        embedded_meta = self._extract_embedded_company_json(soup, clean_slug)

        batch = self._extract_batch(
            page_text=page_text,
            seed_meta=seed_meta,
            embedded_meta=embedded_meta,
        )

        if self.config.target_batches and batch not in self.config.target_batches:
            logger.debug("Skipping %s: batch %s not in %s", clean_slug, batch, self.config.target_batches)
            return None

        status = self._extract_status(soup, embedded_meta)
        if self.config.required_status and status.lower() != self.config.required_status.lower():
            return None

        company_name = self._extract_company_name(soup=soup, slug=clean_slug, seed_meta=seed_meta)

        meta_desc = soup.find("meta", attrs={"name": "description"})
        one_liner = (
            (meta_desc.get("content", "").strip() if meta_desc else "")
            or seed_meta.get("one_liner")
            or ""
        )

        website_url = self._extract_website_url(soup, slug=clean_slug)
        verticals = self._match_vertical_names(page_text)

        excluded, reason = self.config.is_excluded(tags=verticals, text=one_liner)
        if excluded:
            logger.debug("Skipping %s due to exclusion (%s)", clean_slug, reason)
            return None

        team_size = self._parse_team_size(page_text)
        is_hiring = self._extract_is_hiring(
            soup=soup,
            slug=clean_slug,
            seed_meta=seed_meta,
            embedded_meta=embedded_meta,
        )

        icp_score, icp_tier = self._score_icp(
            batch=batch,
            verticals=verticals,
            team_size=team_size,
            is_hiring=is_hiring,
            one_liner=one_liner,
        )

        founders = self._extract_founders_from_soup(soup)

        return {
            "company": {
                "slug": clean_slug,
                "yc_url": yc_url,
                "company_name": company_name,
                "batch": batch,
                "status": status,
                "one_liner": one_liner,
                "verticals": verticals,
                "website_url": website_url,
                "team_size": team_size,
                "is_hiring": is_hiring,
                "icp_score": icp_score,
                "icp_tier": icp_tier,
                "raw_meta": {
                    "discovered_via": "algolia" if seed_meta.get("batch") else "yc_directory",
                    "founders_found": len(founders),
                },
            },
            "founders": founders,
        }

    def run(
        self,
        extra_slugs: list[str] | None = None,
        limit: int | None = None,
        log_every: int = 10,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        logger.info(
            "Starting YC directory scrape (batches=%s, proxies=%d, delay=%.1f-%.1fs)",
            self.config.target_batches,
            len(self.config.proxy_urls),
            self.config.min_delay_sec,
            self.config.max_delay_sec,
        )

        candidates = self._discover_slugs_from_algolia()
        if not candidates:
            candidates = self._discover_slugs_from_directory()

        if extra_slugs:
            existing_slugs = {c["slug"] for c in candidates}
            for s in extra_slugs:
                clean_slug = self._normalize_slug(s)
                if clean_slug and clean_slug not in existing_slugs:
                    existing_slugs.add(clean_slug)
                    candidates.append(
                        {
                            "slug": clean_slug,
                            "yc_url": f"{BASE_URL}/companies/{clean_slug}",
                            "is_hiring": False,
                        }
                    )

        if self.config.prioritize_hiring_first:
            candidates.sort(key=lambda c: (0 if c.get("is_hiring") else 1, c["slug"]))

        if limit is not None:
            if limit < 0:
                raise ValueError(f"limit must be >= 0, got {limit}")
            candidates = candidates[:limit]

        total = len(candidates)
        logger.info("Discovered %d candidate slugs to process", total)

        processed_companies = 0
        skipped_companies = 0
        error_companies = 0
        saved_founders = 0

        for idx, item in enumerate(candidates, start=1):
            slug = item["slug"]
            try:
                parsed = self.parse_company_page(slug=slug, seed_meta=item)
            except Exception:
                error_companies += 1
                logger.exception("Unhandled error parsing slug=%s (%d/%d)", slug, idx, total)
                self._sleep_jitter(multiplier=1.0)
                continue

            self._sleep_jitter(multiplier=1.0)

            if not parsed:
                skipped_companies += 1
                continue

            company_data = parsed["company"]
            company_id = self.db.upsert_company(company_data)
            processed_companies += 1

            if company_data["is_hiring"]:
                self.db.add_signal(
                    company_id=company_id,
                    signal_type="HIRING_ACTIVE",
                    signal_title=f"Active hiring detected on YC profile ({company_data['company_name']})",
                    signal_url=company_data["yc_url"],
                )

            for founder in parsed["founders"]:
                self.db.upsert_founder(company_id=company_id, founder=founder)
                saved_founders += 1

            if log_every > 0 and (idx % log_every == 0 or idx == total):
                elapsed = time.monotonic() - started_at
                logger.info(
                    "Progress [%d/%d] | saved=%d skipped=%d errors=%d | elapsed=%.1fs",
                    idx,
                    total,
                    processed_companies,
                    skipped_companies,
                    error_companies,
                    elapsed,
                )

        elapsed_total = round(time.monotonic() - started_at, 2)
        stats: dict[str, Any] = self.db.summary_counts()
        stats.update(
            {
                "candidates_discovered": total,
                "processed_this_run": processed_companies,
                "skipped_this_run": skipped_companies,
                "errors_this_run": error_companies,
                "founders_upserted_this_run": saved_founders,
                "elapsed_sec": elapsed_total,
            }
        )
        logger.info("YC scrape complete in %.2fs: %s", elapsed_total, stats)
        return stats


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run YCDirectoryScraper for ADL-SignalForge.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of candidate company slugs to process (e.g., --limit 15).",
    )
    parser.add_argument(
        "--slugs",
        type=str,
        default="",
        help="Comma-separated extra YC company slugs to include (e.g., --slugs kastle,browser-use).",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=10,
        help="Log progress every N candidates (default: 10).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_cli_args()
    parsed_slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] or None

    cfg = ScraperConfig()
    database = YCLeadsDatabase(cfg.db_path)
    scraper = YCDirectoryScraper(config=cfg, db=database)
    result = scraper.run(extra_slugs=parsed_slugs, limit=args.limit, log_every=args.log_every)
    print(json.dumps(result, indent=2))