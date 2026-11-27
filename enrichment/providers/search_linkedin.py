"""Stage 1 zero-cost public search extractor for LinkedIn company & decision-maker profiles."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..base import (
    ContactRecord,
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    normalize_domain,
)

logger = logging.getLogger(__name__)


class SearchBackendError(RuntimeError):
    """Raised when an injected `search_backend` fails during query execution."""

    def __init__(self, query: str, cause: Exception) -> None:
        self.query = query
        self.cause = cause
        super().__init__(
            f"search_backend failed for query={query!r} ({type(cause).__name__}: {cause})"
        )


DEFAULT_DECISION_MAKER_TITLES: tuple[str, ...] = (
    "Founder",
    "Co-Founder",
    "CEO",
    "CMO",
    "VP Marketing",
    "Head of Growth",
    "Head of Sales",
    "Managing Director",
)

ROLE_EXTRACT_PATTERN = re.compile(
    r"\b("
    r"co-founder\s*(?:&|and)?\s*ceo|founder\s*(?:&|and)?\s*ceo|"
    r"co-founder|founder|ceo|chief executive officer|"
    r"cmo|chief marketing officer|vp of marketing|vp marketing|head of marketing|"
    r"head of growth|vp of growth|vp growth|"
    r"head of sales|vp of sales|vp sales|cro|chief revenue officer|"
    r"managing director|director of sales|director of marketing|partner|president|coo|cto"
    r")\b",
    re.IGNORECASE,
)

HEADCOUNT_SNIPPET_PATTERN = re.compile(
    r"\b(\d[\d,]*\s*(?:\+|\s*[-–—]\s*\d[\d,]*)?\s*employees)\b",
    re.IGNORECASE,
)

FOLLOWER_SNIPPET_PATTERN = re.compile(
    r"\b(\d[\d,]*\s*(?:\+|\s*[-–—]\s*\d[\d,]*)?\s*followers)\b",
    re.IGNORECASE,
)

CHALLENGE_MARKERS: tuple[str, ...] = (
    "anomaly.js",
    "anomaly-modal",
    "challenge-form",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "unusual traffic",
    "bots use duckduckgo",
    "select all squares",
)

GENERIC_COMMON_TOKENS: frozenset[str] = frozenset(
    {
        "nova",
        "peak",
        "scale",
        "atlas",
        "apex",
        "pulse",
        "nexus",
        "vanguard",
        "zenith",
        "summit",
        "horizon",
        "pioneer",
        "global",
        "digital",
        "solutions",
        "systems",
        "group",
        "media",
        "labs",
        "data",
        "cloud",
        "tech",
        "soft",
        "works",
        "hub",
        "hq",
    }
)

RESERVED_LINKEDIN_SLUGS: frozenset[str] = frozenset(
    {
        "unavailable",
        "login",
        "uas",
        "checkpoint",
        "home",
        "feed",
        "jobs",
        "learning",
        "messaging",
        "notifications",
        "mynetwork",
    }
)

SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9\-_%]+$")

PRONOUNS_PATTERN = re.compile(
    r"\(\s*(?:she/her|he/him|they/them|xe/xir|ze/zir|any pronouns)\s*\)",
    re.IGNORECASE,
)

CREDENTIALS_SUFFIX_PATTERN = re.compile(
    r"(?:,\s*|\s+)(?:MBA|EMBA|PhD|Ph\.D\.|MD|CPA|CFA|PMP|CISSP|PE|JD|BSc|MSc|M\.Sc\.|B\.Sc\.)\b\.?",
    re.IGNORECASE,
)

EMOJI_OR_DECORATION_PATTERN = re.compile(
    r"[\u2600-\u27BF\U0001F300-\U0001FAFF\u2702-\u27B0]+",
)

TITLE_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\||–|—|•|·)\s*|\s+[\-–—]\s+")


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


SearchBackendFn = Callable[[str, int], Awaitable[list[SearchHit]]]


@dataclass
class SearchLinkedInConfig:
    max_results_per_query: int = 8
    max_roles_per_query: int = 12
    timeout_seconds: float = 12.0
    max_retries: int = 2
    inter_query_delay_seconds: float = 0.8
    swallow_backend_errors: bool = False
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _bucket_employee_count(count: int) -> str:
    """Map an integer employee estimate into a normalized company-size bucket."""
    if count <= 0:
        return ""
    if count <= 10:
        return "1-10"
    if count <= 50:
        return "11-50"
    if count <= 200:
        return "51-200"
    if count <= 500:
        return "201-500"
    if count <= 1000:
        return "501-1000"
    if count <= 5000:
        return "1001-5000"
    return "5001+"


def normalize_headcount_size(raw_headcount: str | None) -> str | None:
    """
    Parse a headcount phrase (e.g. '500+ employees', '50-200 employees', '1,200 employees')
    into a standardized size bucket. Returns None for non-employee metrics or unparseable text.
    """
    if not raw_headcount:
        return None
    cleaned = _clean_text(raw_headcount).lower()
    if "follower" in cleaned:
        return None
    if "employee" not in cleaned:
        return None

    # Check explicit range like "50-200 employees" or "50 – 200 employees"
    range_match = re.search(r"(\d[\d,]*)\s*(?:\+|[-–—])\s*(\d[\d,]*)", cleaned)
    if range_match:
        low = int(range_match.group(1).replace(",", ""))
        high = int(range_match.group(2).replace(",", ""))
        midpoint = max(1, (low + high) // 2)
        return _bucket_employee_count(midpoint)

    plus_match = re.search(r"(\d[\d,]*)\s*\+", cleaned)
    if plus_match:
        val = int(plus_match.group(1).replace(",", ""))
        # Slightly step up above the floor (e.g., 500+ -> 501)
        return _bucket_employee_count(val + 1)

    single_match = re.search(r"(\d[\d,]*)", cleaned)
    if single_match:
        val = int(single_match.group(1).replace(",", ""))
        return _bucket_employee_count(val)

    return None


def _strip_serp_noise(text: str) -> str:
    """Remove LinkedIn suffixes, pronoun tags, credential suffixes, and emojis."""
    cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", text, flags=re.IGNORECASE)
    cleaned = PRONOUNS_PATTERN.sub("", cleaned)
    cleaned = EMOJI_OR_DECORATION_PATTERN.sub(" ", cleaned)
    return _clean_text(cleaned)


def _clean_person_name(raw_segment: str) -> str:
    """Extract and sanitize a candidate person name from a SERP title segment."""
    cleaned = _strip_serp_noise(raw_segment)
    cleaned = re.split(r"\s+(?:at|@)\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = CREDENTIALS_SUFFIX_PATTERN.sub("", cleaned).strip(" ,-–—|•·")
    cleaned = _clean_text(cleaned)
    if not cleaned or len(cleaned) > 70:
        return ""
    if ROLE_EXTRACT_PATTERN.fullmatch(cleaned):
        return ""
    if re.search(r"[/\\0-9]", cleaned):
        return ""
    return cleaned


def _extract_role_from_segments_and_snippet(
    segments: list[str],
    snippet: str,
    company_tokens: list[str],
) -> str | None:
    """Extract a clean role/title from remaining title segments or snippet text."""
    for seg in segments[1:]:
        cleaned_seg = _strip_serp_noise(seg)
        if not cleaned_seg:
            continue
        role_match = ROLE_EXTRACT_PATTERN.search(cleaned_seg)
        if role_match:
            start = max(0, role_match.start() - 20)
            end = min(len(cleaned_seg), role_match.end() + 45)
            candidate = _clean_text(cleaned_seg[start:end]).strip(" ,-–—|•·")
            candidate = re.split(r"\s+(?:at|@)\s+", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            return candidate[:90] if candidate else role_match.group(0)

    cleaned_snippet = _strip_serp_noise(snippet)
    snippet_role = ROLE_EXTRACT_PATTERN.search(cleaned_snippet)
    if snippet_role:
        start = max(0, snippet_role.start() - 15)
        end = min(len(cleaned_snippet), snippet_role.end() + 45)
        candidate = _clean_text(cleaned_snippet[start:end]).strip(" ,-–—|•·")
        candidate = re.split(r"\s+(?:at|@)\s+", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if candidate and not _matches_company(candidate, company_tokens):
            return candidate[:90]
        return snippet_role.group(0)

    if len(segments) >= 2:
        fallback = _strip_serp_noise(segments[1])
        fallback = re.split(r"\s+(?:at|@)\s+", fallback, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,-–—|•·")
        if fallback and len(fallback) <= 90 and not _matches_company(fallback, company_tokens):
            return fallback

    return None


def _is_challenge_or_block_response(status_code: int, html: str) -> bool:
    """Detect HTTP block statuses or search challenge/CAPTCHA/anomaly pages."""
    if status_code in {403, 429, 202}:
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def _unwrap_redirect_url(raw_href: str) -> str:
    href = raw_href.strip()
    if "duckduckgo.com/l/?" in href or href.startswith("/l/?"):
        parsed = urlparse(href)
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg and uddg[0]:
            return unquote(uddg[0])
    return href


def _extract_linkedin_root_slug(
    url: str,
    section: str,
    *,
    allow_subpaths: bool = False,
) -> str | None:
    """
    Extract a validated root slug for `<section>/<slug>` on a LinkedIn host while
    rejecting subpaths (e.g., `/in/<slug>/posts` or `/company/<slug>/jobs`) unless
    explicitly allowed, and preserving the slug's original case.
    """
    cleaned = _unwrap_redirect_url(url).strip()
    if not cleaned:
        return None

    candidate = cleaned if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned) else f"https://{cleaned}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None

    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 2 or segments[0].lower() != section.lower():
        return None

    if len(segments) > 2 and not allow_subpaths:
        return None

    slug = segments[1]
    if not slug or not SLUG_PATTERN.fullmatch(slug):
        return None
    if slug.lower() in RESERVED_LINKEDIN_SLUGS:
        return None

    return slug


def canonical_linkedin_profile_url(
    url: str,
    *,
    allow_subpaths: bool = False,
) -> str | None:
    """Normalize a LinkedIn profile URL to https://www.linkedin.com/in/<slug> preserving slug case."""
    slug = _extract_linkedin_root_slug(url, "in", allow_subpaths=allow_subpaths)
    if not slug:
        return None
    return f"https://www.linkedin.com/in/{slug}"


def canonical_linkedin_company_url(
    url: str,
    *,
    allow_subpaths: bool = False,
) -> str | None:
    """Normalize a LinkedIn company URL to https://www.linkedin.com/company/<slug> preserving slug case."""
    slug = _extract_linkedin_root_slug(url, "company", allow_subpaths=allow_subpaths)
    if not slug:
        return None
    return f"https://www.linkedin.com/company/{slug}"


def _derive_company_tokens(company_name: str | None, domain: str | None) -> tuple[str, list[str]]:
    """Return primary search label and specificity-filtered matching tokens."""
    tokens: list[str] = []
    if company_name:
        cleaned_name = _clean_text(company_name)
        if cleaned_name:
            lower_full = cleaned_name.lower()
            tokens.append(lower_full)
            stripped_suffix = re.sub(
                r"\b(inc|ltd|limited|llc|corp|corporation|plc|gmbh|co|group|agency)\b\.?",
                "",
                cleaned_name,
                flags=re.IGNORECASE,
            ).strip(" ,.-")
            lower_stripped = stripped_suffix.lower()
            if len(lower_stripped) >= 4 and lower_stripped not in tokens:
                tokens.append(lower_stripped)

    norm_dom = normalize_domain(domain)
    if norm_dom:
        full_dom = norm_dom.lower()
        if full_dom not in tokens:
            tokens.append(full_dom)
        root_label = full_dom.split(".", 1)[0]
        if len(root_label) >= 4 and root_label not in tokens:
            tokens.append(root_label)

    primary_label = (
        _clean_text(company_name)
        if company_name and _clean_text(company_name)
        else (norm_dom.split(".", 1)[0] if norm_dom else "")
    )
    return primary_label, tokens


def _token_matches_text(token: str, haystack: str) -> bool:
    """Match a token against haystack using word boundaries (or domain boundary for domains)."""
    cleaned_tok = token.strip().lower()
    if not cleaned_tok:
        return False

    if "." in cleaned_tok:
        pattern = re.compile(rf"(?<![a-z0-9\-]){re.escape(cleaned_tok)}(?![a-z0-9\-])", re.IGNORECASE)
        return bool(pattern.search(haystack))

    words = cleaned_tok.split()
    if len(words) == 1 and (len(cleaned_tok) < 4 or cleaned_tok in GENERIC_COMMON_TOKENS):
        return False

    pattern = re.compile(rf"\b{re.escape(cleaned_tok)}\b", re.IGNORECASE)
    return bool(pattern.search(haystack))


def _matches_company(text: str, company_tokens: list[str]) -> bool:
    if not company_tokens:
        return True
    haystack = _clean_text(text).lower()
    if not haystack:
        return False
    return any(_token_matches_text(tok, haystack) for tok in company_tokens)


def parse_person_from_hit(
    hit: SearchHit,
    *,
    company_tokens: list[str],
    target_titles: list[str] | None = None,
) -> ContactRecord | None:
    """Parse a ContactRecord from a public LinkedIn /in/ search result."""
    profile_url = canonical_linkedin_profile_url(hit.url)
    if not profile_url:
        return None

    combined = f"{hit.title} | {hit.snippet}"
    if not _matches_company(combined, company_tokens):
        return None

    cleaned_title = _strip_serp_noise(hit.title)
    parts = [p.strip() for p in TITLE_SEGMENT_SPLIT_RE.split(cleaned_title) if p.strip()]

    raw_name = _clean_person_name(parts[0]) if parts else ""
    if not raw_name:
        return None

    role = _extract_role_from_segments_and_snippet(parts, hit.snippet, company_tokens)

    title_filters = [t.strip().lower() for t in (target_titles or []) if t.strip()]
    if title_filters:
        if not role or not any(tf in role.lower() for tf in title_filters):
            return None

    name_tokens = raw_name.split()
    first_name = name_tokens[0] if name_tokens else None
    last_name = " ".join(name_tokens[1:]) if len(name_tokens) > 1 else None

    confidence = 0.78 if role else 0.62
    return ContactRecord(
        full_name=raw_name,
        first_name=first_name,
        last_name=last_name,
        title=role,
        linkedin_url=profile_url,
        verified_status=VerificationStatus.UNVERIFIED,
        confidence=confidence,
        source="search_linkedin",
    )


def parse_search_hits_to_result(
    *,
    company_name: str | None,
    domain: str | None,
    company_hits: list[SearchHit],
    people_hits: list[SearchHit],
    target_titles: list[str] | None = None,
) -> EnrichmentResult | None:
    """Turn company + people search hits into a normalized EnrichmentResult."""
    _, company_tokens = _derive_company_tokens(company_name, domain)

    company_linkedin_url: str | None = None
    company_snippet: str | None = None
    raw_headcount_hint: str | None = None
    normalized_company_size: str | None = None
    follower_hint: str | None = None
    evidence_urls: list[str] = []

    for hit in company_hits:
        comp_url = canonical_linkedin_company_url(hit.url)
        if not comp_url:
            continue
        combined = f"{hit.title} | {hit.snippet}"
        if not _matches_company(combined, company_tokens):
            continue
        company_linkedin_url = comp_url
        company_snippet = _clean_text(hit.snippet)[:350] or None
        hc_match = HEADCOUNT_SNIPPET_PATTERN.search(combined)
        if hc_match:
            raw_headcount_hint = _clean_text(hc_match.group(1))
            normalized_company_size = normalize_headcount_size(raw_headcount_hint)
        fl_match = FOLLOWER_SNIPPET_PATTERN.search(combined)
        if fl_match:
            follower_hint = _clean_text(fl_match.group(1))
        if comp_url not in evidence_urls:
            evidence_urls.append(comp_url)
        break

    contacts_by_url_key: dict[str, ContactRecord] = {}
    for hit in people_hits:
        contact = parse_person_from_hit(
            hit,
            company_tokens=company_tokens,
            target_titles=target_titles,
        )
        if contact and contact.linkedin_url:
            dedup_key = contact.linkedin_url.lower()
            if dedup_key not in contacts_by_url_key:
                contacts_by_url_key[dedup_key] = contact
                if contact.linkedin_url not in evidence_urls:
                    evidence_urls.append(contact.linkedin_url)

    if not company_linkedin_url and not contacts_by_url_key:
        return None

    company_patch: dict[str, Any] = {
        "extra": {
            "linkedin_intelligence": {
                "company_linkedin_url": company_linkedin_url,
                "company_snippet": company_snippet,
                "headcount_hint": raw_headcount_hint,
                "follower_hint": follower_hint,
                "evidence_urls": evidence_urls,
            }
        }
    }
    if domain:
        company_patch["domain"] = normalize_domain(domain)
    if normalized_company_size:
        company_patch["company_size"] = normalized_company_size

    confidence = 0.76 if contacts_by_url_key else 0.65

    return EnrichmentResult(
        source="search_linkedin",
        stage="stage_1",
        confidence=confidence,
        verified_status=VerificationStatus.UNVERIFIED,
        credits_used=0.0,
        company_patch=company_patch,
        contacts=list(contacts_by_url_key.values()),
        raw_payload={
            "company_linkedin_url": company_linkedin_url,
            "contacts_found": len(contacts_by_url_key),
            "evidence_urls": evidence_urls,
        },
    )


def _build_role_clauses(roles: list[str], max_per_query: int) -> list[str]:
    """Build role search clauses without silently dropping configured titles."""
    cleaned_roles = [r.strip() for r in roles if r and r.strip()]
    if not cleaned_roles:
        cleaned_roles = list(DEFAULT_DECISION_MAKER_TITLES)

    chunk_size = max(1, max_per_query)
    clauses: list[str] = []
    for idx in range(0, len(cleaned_roles), chunk_size):
        chunk = cleaned_roles[idx : idx + chunk_size]
        clauses.append(" OR ".join(f'"{r}"' for r in chunk))
    return clauses


class SearchLinkedInProvider:
    """Stage 1 public SERP extractor for LinkedIn leadership & company signals."""

    def __init__(
        self,
        config: SearchLinkedInConfig | None = None,
        *,
        search_backend: SearchBackendFn | None = None,
    ) -> None:
        self.config = config or SearchLinkedInConfig()
        self.search_backend = search_backend

    async def _default_ddg_search(
        self,
        query: str,
        max_results: int,
        client: httpx.AsyncClient | None = None,
    ) -> list[SearchHit]:
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)

        async def _execute_with_client(active_client: httpx.AsyncClient) -> list[SearchHit]:
            for attempt in range(self.config.max_retries + 1):
                try:
                    resp = await active_client.post("https://html.duckduckgo.com/html/", data={"q": query})
                    body = resp.text
                    if _is_challenge_or_block_response(resp.status_code, body):
                        logger.warning(
                            "DuckDuckGo search blocked or challenged (status=%s) for query=%r",
                            resp.status_code,
                            query,
                        )
                        return []

                    if resp.status_code != 200:
                        if attempt == self.config.max_retries:
                            return []
                        await asyncio.sleep(0.6 * (2**attempt))
                        continue

                    soup = BeautifulSoup(body, "html.parser")
                    hits: list[SearchHit] = []
                    for block in soup.select(".result"):
                        link = block.select_one("a.result__a")
                        if not link or not link.get("href"):
                            continue
                        snippet_el = block.select_one(".result__snippet")
                        title = _clean_text(link.get_text(" ", strip=True))
                        href = _unwrap_redirect_url(str(link["href"]))
                        snippet = _clean_text(snippet_el.get_text(" ", strip=True)) if snippet_el else ""
                        if title and href:
                            hits.append(SearchHit(title=title, url=href, snippet=snippet))
                        if len(hits) >= max_results:
                            break
                    return hits
                except httpx.HTTPError as exc:
                    logger.debug(
                        "DuckDuckGo search HTTP error on attempt %d/%d for query=%r: %s",
                        attempt + 1,
                        self.config.max_retries + 1,
                        query,
                        exc,
                    )
                    if attempt == self.config.max_retries:
                        return []
                    await asyncio.sleep(0.6 * (2**attempt))
            return []

        if client is not None:
            return await _execute_with_client(client)

        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as owned_client:
            return await _execute_with_client(owned_client)

    async def _run_query(self, query: str, client: httpx.AsyncClient | None = None) -> list[SearchHit]:
        if self.search_backend is not None:
            try:
                return await self.search_backend(query, self.config.max_results_per_query)
            except Exception as exc:
                logger.error(
                    "Custom search_backend failed for query=%r (%s: %s)",
                    query,
                    type(exc).__name__,
                    exc,
                )
                if self.config.swallow_backend_errors:
                    return []
                raise SearchBackendError(query, exc) from exc
        return await self._default_ddg_search(query, self.config.max_results_per_query, client=client)

    async def fetch_lead_signals(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> list[EnrichmentResult]:
        domain = normalize_domain(lead.domain)
        primary_label, _ = _derive_company_tokens(lead.company_name, domain)
        if not primary_label and not domain:
            return []

        query_label = primary_label or domain or ""
        roles = policy.target_titles if policy.target_titles else list(DEFAULT_DECISION_MAKER_TITLES)
        role_clauses = _build_role_clauses(roles, self.config.max_roles_per_query)

        company_terms = query_label
        if domain and domain.split(".", 1)[0].lower() != query_label.lower():
            company_terms = f"{query_label} {domain}"

        company_query = f"site:linkedin.com/company {company_terms}"
        people_queries = [f"site:linkedin.com/in {query_label} ({clause})" for clause in role_clauses]

        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)

        people_hits: list[SearchHit] = []
        if self.search_backend is not None:
            company_hits = await self._run_query(company_query)
            for pq in people_queries:
                if self.config.inter_query_delay_seconds > 0:
                    await asyncio.sleep(self.config.inter_query_delay_seconds)
                people_hits.extend(await self._run_query(pq))
        else:
            async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
                company_hits = await self._run_query(company_query, client=client)
                for pq in people_queries:
                    if self.config.inter_query_delay_seconds > 0:
                        await asyncio.sleep(self.config.inter_query_delay_seconds)
                    people_hits.extend(await self._run_query(pq, client=client))

        result = parse_search_hits_to_result(
            company_name=lead.company_name,
            domain=domain,
            company_hits=company_hits,
            people_hits=people_hits,
            target_titles=policy.target_titles,
        )
        return [result] if result is not None else []

    async def __call__(self, lead: LeadRecord, policy: EnrichmentPolicy) -> list[EnrichmentResult]:
        return await self.fetch_lead_signals(lead, policy)


__all__ = [
    "DEFAULT_DECISION_MAKER_TITLES",
    "SearchBackendError",
    "SearchBackendFn",
    "SearchHit",
    "SearchLinkedInConfig",
    "SearchLinkedInProvider",
    "canonical_linkedin_company_url",
    "canonical_linkedin_profile_url",
    "normalize_headcount_size",
    "parse_person_from_hit",
    "parse_search_hits_to_result",
]