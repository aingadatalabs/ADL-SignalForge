"""Stage 1 first-party web crawler and structured signal extractor."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..base import (
    ContactRecord,
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    normalize_domain,
    normalize_email,
    normalize_linkedin_url,
)

logger = logging.getLogger(__name__)

FrankensurfFetcher = Callable[[str], Awaitable[str | None]]


class FrankensurfTransportError(RuntimeError):
    """Raised when `frankensurf_fetch` fails and fallback to direct httpx is disabled/disallowed."""


DEFAULT_PATHS: tuple[str, ...] = (
    "/",
    "/about",
    "/about-us",
    "/services",
    "/solutions",
    "/team",
    "/leadership",
    "/company",
    "/careers",
    "/jobs",
    "/case-studies",
    "/contact",
)

LEADERSHIP_ROLE_PATTERN = re.compile(
    r"\b("
    r"founder|co-founder|ceo|chief executive officer|"
    r"cmo|chief marketing officer|vp of marketing|vp marketing|head of marketing|"
    r"head of growth|vp of growth|vp growth|"
    r"head of sales|vp of sales|vp sales|cro|chief revenue officer|"
    r"managing director|director|president|coo|cto"
    r")\b",
    re.IGNORECASE,
)

INTENT_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "hiring_sales": re.compile(
        r"\b(hiring|open roles?|join our team)\b[^\n.!?]{0,45}\b(sales|account executive|ae|sdr|bdr|business development)\b",
        re.IGNORECASE,
    ),
    "hiring_marketing_growth": re.compile(
        r"\b(hiring|open roles?|join our team)\b[^\n.!?]{0,45}\b(marketing|growth|demand gen|seo|paid media|content)\b",
        re.IGNORECASE,
    ),
    "funding_or_expansion": re.compile(
        r"\b(raised|series [abc]|seed round|funding|expanded? into|new office|acquired)\b[^\n.!?]{0,45}",
        re.IGNORECASE,
    ),
    "new_offering_or_case_study": re.compile(
        r"\b(case stud(?:y|ies)|client results?|launched?|introducing|new service|platform)\b[^\n.!?]{0,45}",
        re.IGNORECASE,
    ),
}

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

ASSET_EXTENSIONS: frozenset[str] = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
        "svg",
        "ico",
        "bmp",
        "avif",
        "css",
        "js",
        "mjs",
        "map",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "mp4",
        "webm",
        "mp3",
        "wav",
        "pdf",
        "zip",
    }
)

BLOCKED_EMAIL_LOCAL_PREFIXES: tuple[str, ...] = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "webmaster@",
    "abuse@",
    "privacy@",
    "security@",
)

BLOCKED_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "localhost",
        "sentry.io",
        "wixpress.com",
    }
)

TITLE_SEPARATOR_RE = re.compile(r"\s+(?:\||–|—|•|·|/|-)\s+|\s+(?:\||–|—|•|·)\s*")

BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
    }
)

BLOCKED_HOSTNAME_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".intra",
    ".corp",
    ".home",
    ".lan",
)

REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
JSON_LD_OPEN_RE = re.compile(r"<script\b[^>]*type\s*=\s*['\"]?application/ld\+json['\"]?[^>]*>", re.IGNORECASE)
JSON_LD_CLOSE_RE = re.compile(r"</script\s*>", re.IGNORECASE)


def _deep_merge_extra(base_extra: dict[str, Any], incoming_extra: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `incoming_extra` into a copy of `base_extra` without losing sibling keys."""
    merged = copy.deepcopy(base_extra)
    for key, val in incoming_extra.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge_extra(merged[key], val)
        else:
            merged[key] = copy.deepcopy(val)
    return merged


def _build_extra_payload(
    *,
    commercial_intelligence: dict[str, Any],
    existing_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an `extra` dict preserving existing sibling keys on the lead."""
    incoming = {"commercial_intelligence": commercial_intelligence}
    if existing_extra:
        return _deep_merge_extra(existing_extra, incoming)
    return copy.deepcopy(incoming)


def _extract_company_from_title(raw_title: str) -> str | None:
    """Clean a <title> string without splitting hyphenated words like 'Coca-Cola'."""
    cleaned = _clean_text(raw_title)
    if not cleaned:
        return None
    parts = [p.strip() for p in TITLE_SEPARATOR_RE.split(cleaned) if p.strip()]
    if not parts:
        return cleaned
    generic_prefixes = {"home", "about", "about us", "contact", "services", "careers", "blog", "news"}
    if len(parts) > 1 and parts[0].lower() in generic_prefixes:
        return parts[1]
    return parts[0]


def _is_noise_or_asset_email(email: str | None) -> bool:
    """Return True if an extracted string is an asset filename or non-prospect/noise email."""
    if not email:
        return True
    cleaned = email.strip().lower()
    if "@" not in cleaned:
        return True
    local_part, domain_part = cleaned.rsplit("@", 1)
    if not local_part or not domain_part or "." not in domain_part:
        return True

    if cleaned.startswith(BLOCKED_EMAIL_LOCAL_PREFIXES):
        return True

    if domain_part in BLOCKED_EMAIL_DOMAINS:
        return True

    tld = domain_part.rsplit(".", 1)[-1]
    if tld in ASSET_EXTENSIONS:
        return True
    if re.search(r"@\d+x\.", cleaned):
        return True

    return False


def _is_disallowed_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP address is loopback, private, link-local, multicast, or reserved."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_disallowed_ip(addr.ipv4_mapped)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or getattr(addr, "is_site_local", False)
    )


async def _is_safe_target_url(url: str) -> bool:
    """Validate URL scheme, hostname, and resolved IP addresses against SSRF targets."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False

    if host in BLOCKED_HOSTNAMES or any(host.endswith(suf) for suf in BLOCKED_HOSTNAME_SUFFIXES):
        return False

    try:
        literal_ip = ipaddress.ip_address(host)
        return not _is_disallowed_ip(literal_ip)
    except ValueError:
        pass

    if "." not in host:
        return False

    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in addr_info:
        sockaddr = item[4]
        if not sockaddr:
            continue
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved_ips.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue

    if not resolved_ips:
        return False

    return all(not _is_disallowed_ip(ip) for ip in resolved_ips)


def _trim_incomplete_markup_tail(html: str, *, was_truncated_by_bytes: bool) -> str:
    """
    Prevent mid-tag, mid-entity, or mid-JSON-LD script corruption when content is byte-capped.
    """
    if not was_truncated_by_bytes or not html:
        return html

    cleaned = html

    open_matches = list(JSON_LD_OPEN_RE.finditer(cleaned))
    if open_matches:
        last_open = open_matches[-1]
        closing_after = JSON_LD_CLOSE_RE.search(cleaned, pos=last_open.end())
        if closing_after is None:
            cleaned = cleaned[: last_open.start()]

    last_lt = cleaned.rfind("<")
    last_gt = cleaned.rfind(">")
    if last_lt > last_gt:
        cleaned = cleaned[:last_lt]

    last_amp = cleaned.rfind("&")
    last_semi = cleaned.rfind(";", max(0, last_amp)) if last_amp != -1 else -1
    if last_amp != -1 and last_semi < last_amp:
        tail_fragment = cleaned[last_amp:]
        if re.fullmatch(r"&[A-Za-z0-9#]{1,16}", tail_fragment):
            cleaned = cleaned[:last_amp]

    return cleaned


def _cap_html_bytes(raw_bytes: bytes, max_bytes: int) -> str:
    """Enforce a strict UTF-8 byte ceiling and repair truncated markup tails."""
    if max_bytes <= 0:
        return ""
    was_truncated = len(raw_bytes) > max_bytes
    bounded_bytes = raw_bytes[:max_bytes] if was_truncated else raw_bytes
    decoded = bounded_bytes.decode("utf-8", errors="replace")
    return _trim_incomplete_markup_tail(decoded, was_truncated_by_bytes=was_truncated)


@dataclass
class CrawlConfig:
    max_pages_per_domain: int = 8
    max_concurrent_pages: int = 4
    domain_timeout_seconds: float = 20.0
    max_bytes_per_page: int = 1_500_000
    timeout_seconds: float = 12.0
    max_retries: int = 2
    max_redirects: int = 5
    fallback_to_httpx_on_error: bool = True
    require_frankensurf_transport: bool = False
    user_agent: str = "ADL-SignalForge-Crawler/1.0 (+https://aingadatalabs.com)"
    paths: tuple[str, ...] = DEFAULT_PATHS


@dataclass
class _PageSnapshot:
    url: str
    html: str


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_person_key(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", " ", name.strip().lower())


def _find_matching_contact(
    contacts: list[ContactRecord],
    candidate: ContactRecord,
) -> int | None:
    """Match an extracted contact against existing contacts by email, LinkedIn URL, or full name."""
    cand_email = normalize_email(candidate.email)
    cand_li = normalize_linkedin_url(candidate.linkedin_url)
    cand_name = _normalize_person_key(candidate.full_name)

    for idx, existing in enumerate(contacts):
        ex_email = normalize_email(existing.email)
        ex_li = normalize_linkedin_url(existing.linkedin_url)
        ex_name = _normalize_person_key(existing.full_name)

        if cand_email and ex_email and cand_email == ex_email:
            return idx
        if cand_li and ex_li and cand_li == ex_li:
            return idx
        if cand_name and ex_name and cand_name == ex_name:
            return idx

    return None


def _upsert_contact(contacts: list[ContactRecord], incoming: ContactRecord) -> None:
    """Insert or enrich an existing ContactRecord without creating fragmented duplicates."""
    norm_li = normalize_linkedin_url(incoming.linkedin_url)
    cand = incoming.model_copy(
        update={
            "email": normalize_email(incoming.email),
            "linkedin_url": norm_li,
            "full_name": _clean_text(incoming.full_name) if incoming.full_name else None,
            "title": _clean_text(incoming.title) if incoming.title else None,
        }
    )
    if not (cand.email or cand.linkedin_url or cand.full_name):
        return

    match_idx = _find_matching_contact(contacts, cand)
    if match_idx is None:
        contacts.append(cand)
        return

    existing = contacts[match_idx]
    for field_name in ("full_name", "title", "email", "phone", "linkedin_url"):
        new_val = getattr(cand, field_name)
        cur_val = getattr(existing, field_name)
        if new_val and not cur_val:
            setattr(existing, field_name, new_val)
        elif new_val and cur_val and cand.confidence > existing.confidence:
            setattr(existing, field_name, new_val)

    if cand.confidence > existing.confidence:
        existing.confidence = cand.confidence
        if cand.source:
            existing.source = cand.source
    elif not existing.source and cand.source:
        existing.source = cand.source


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    blocks.extend(g for g in item["@graph"] if isinstance(g, dict))
                else:
                    blocks.append(item)
    return blocks


def parse_pages_to_result(
    domain: str,
    pages: list[_PageSnapshot],
    *,
    target_titles: list[str] | None = None,
    existing_extra: dict[str, Any] | None = None,
) -> EnrichmentResult:
    """Pure HTML/JSON-LD parser turning crawled pages into structured commercial signals."""
    company_name: str | None = None
    description: str | None = None
    location: str | None = None
    services_found: list[str] = []
    intent_signals: dict[str, list[str]] = {k: [] for k in INTENT_SIGNAL_PATTERNS}
    evidence_urls: list[str] = []
    contacts: list[ContactRecord] = []
    title_filters = [t.strip().lower() for t in (target_titles or []) if t.strip()]

    for page in pages:
        evidence_urls.append(page.url)
        soup = BeautifulSoup(page.html, "html.parser")

        # 1. JSON-LD structured entities
        for obj in _extract_json_ld(soup):
            obj_type = obj.get("@type")
            types = [obj_type] if isinstance(obj_type, str) else (obj_type or [])
            lowered_types = {str(t).lower() for t in types}

            if "organization" in lowered_types or "corporation" in lowered_types or "localbusiness" in lowered_types:
                if not company_name and isinstance(obj.get("name"), str):
                    company_name = _clean_text(obj["name"])
                if not description and isinstance(obj.get("description"), str):
                    description = _clean_text(obj["description"])
                addr = obj.get("address")
                if not location and isinstance(addr, dict):
                    parts = [
                        addr.get("addressLocality"),
                        addr.get("addressRegion"),
                        addr.get("addressCountry"),
                    ]
                    loc_str = ", ".join(str(p).strip() for p in parts if p)
                    if loc_str:
                        location = loc_str

            if "person" in lowered_types:
                person_name = _clean_text(str(obj.get("name") or "")) or None
                job_title = _clean_text(str(obj.get("jobTitle") or "")) or None
                raw_email = normalize_email(obj.get("email"))
                email = None if _is_noise_or_asset_email(raw_email) else raw_email
                if person_name or email:
                    _upsert_contact(
                        contacts,
                        ContactRecord(
                            full_name=person_name,
                            title=job_title,
                            email=email,
                            verified_status=VerificationStatus.UNVERIFIED,
                            confidence=0.72,
                            source="web_crawler:json_ld",
                        ),
                    )

        # 2. Meta tags & headings
        if not company_name:
            og_site = soup.find("meta", attrs={"property": "og:site_name"})
            if og_site and og_site.get("content"):
                company_name = _clean_text(str(og_site["content"]))
            elif soup.title and soup.title.string:
                company_name = _extract_company_from_title(soup.title.string)

        if not description:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                description = _clean_text(str(meta_desc["content"]))

        # Strip non-content elements for text/signal extraction
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        body_text = _clean_text(soup.get_text(" ", strip=True))

        # Services / offerings from headings & list items on service/about pages
        path_lower = urlparse(page.url).path.lower()
        if any(k in path_lower for k in ("service", "solution", "offering", "what-we-do", "capabilities")):
            for heading in soup.find_all(["h2", "h3", "li"]):
                txt = _clean_text(heading.get_text(" ", strip=True))
                if 3 <= len(txt) <= 90 and txt not in services_found:
                    services_found.append(txt)
                    if len(services_found) >= 25:
                        break

        # Intent / buying signals ("why approach now?")
        for signal_name, pattern in INTENT_SIGNAL_PATTERNS.items():
            for match in pattern.finditer(body_text):
                snippet = _clean_text(match.group(0))[:220]
                if snippet and snippet not in intent_signals[signal_name]:
                    intent_signals[signal_name].append(snippet)
                    if len(intent_signals[signal_name]) >= 5:
                        break

        # Leadership mentions & LinkedIn profile links
        for link in soup.find_all("a", href=True):
            raw_href = str(link["href"]).strip()
            anchor_text = _clean_text(link.get_text(" ", strip=True))
            if "linkedin.com/in/" in raw_href.lower():
                clean_li = normalize_linkedin_url(raw_href)
                if not clean_li:
                    continue
                context_text = _clean_text(link.parent.get_text(" ", strip=True)) if link.parent else anchor_text
                role_match = LEADERSHIP_ROLE_PATTERN.search(context_text)
                inferred_title = role_match.group(0) if role_match else None
                candidate_name = anchor_text if anchor_text and len(anchor_text) <= 80 else None
                if title_filters:
                    if not inferred_title or not any(tf in inferred_title.lower() for tf in title_filters):
                        continue
                _upsert_contact(
                    contacts,
                    ContactRecord(
                        full_name=candidate_name,
                        title=inferred_title,
                        linkedin_url=clean_li,
                        verified_status=VerificationStatus.UNVERIFIED,
                        confidence=0.65,
                        source="web_crawler:linkedin_link",
                    ),
                )

        # Public non-generic emails (skip noise, asset filenames, and blocked domains)
        for email_match in EMAIL_PATTERN.findall(body_text):
            cleaned_email = normalize_email(email_match)
            if _is_noise_or_asset_email(cleaned_email):
                continue
            _upsert_contact(
                contacts,
                ContactRecord(
                    email=cleaned_email,
                    verified_status=VerificationStatus.UNVERIFIED,
                    confidence=0.55,
                    source="web_crawler:page_email",
                ),
            )

    active_intents = {k: v for k, v in intent_signals.items() if v}
    company_patch: dict[str, Any] = {"domain": domain}
    if company_name:
        company_patch["company_name"] = company_name
    if location:
        company_patch["location"] = location

    commercial_intelligence = {
        "summary": description,
        "services_offered": services_found[:15],
        "intent_signals": active_intents,
        "evidence_urls": evidence_urls,
    }
    company_patch["extra"] = _build_extra_payload(
        commercial_intelligence=commercial_intelligence,
        existing_extra=existing_extra,
    )

    confidence = 0.7 if (company_name or services_found or contacts) else 0.25

    return EnrichmentResult(
        source="web_crawler",
        stage="stage_1",
        confidence=confidence,
        verified_status=VerificationStatus.UNVERIFIED,
        credits_used=0.0,
        company_patch=company_patch,
        contacts=contacts,
        raw_payload={"pages_crawled": len(pages), "evidence_urls": evidence_urls},
    )


class WebCrawlerProvider:
    """Stage 1 fetcher for first-party company websites via Frankensurf or httpx + BS4."""

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        frankensurf_fetch: FrankensurfFetcher | None = None,
    ) -> None:
        self.config = config or CrawlConfig()
        self.frankensurf_fetch = frankensurf_fetch

    async def _read_bounded_stream(self, resp: httpx.Response) -> str | None:
        """Stream up to `max_bytes_per_page` UTF-8 bytes without buffering oversized responses."""
        max_bytes = max(0, self.config.max_bytes_per_page)
        if max_bytes == 0:
            return ""

        buf = bytearray()
        truncated = False
        async for chunk in resp.aiter_bytes(chunk_size=16_384):
            if not chunk:
                continue
            remaining = max_bytes - len(buf)
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                buf.extend(chunk[:remaining])
                truncated = True
                break
            buf.extend(chunk)

        decoded = bytes(buf).decode("utf-8", errors="replace")
        return _trim_incomplete_markup_tail(decoded, was_truncated_by_bytes=truncated)

    async def _fetch_html(self, client: httpx.AsyncClient, url: str) -> str | None:
        if not await _is_safe_target_url(url):
            return None

        allow_httpx_fallback = (
            self.config.fallback_to_httpx_on_error and not self.config.require_frankensurf_transport
        )

        if self.frankensurf_fetch is not None:
            try:
                html = await self.frankensurf_fetch(url)
                if html:
                    raw_bytes = html.encode("utf-8", errors="replace")
                    capped = _cap_html_bytes(raw_bytes, self.config.max_bytes_per_page)
                    if capped:
                        return capped
            except Exception as exc:
                logger.warning(
                    "frankensurf_fetch failed for url=%s (%s: %s); fallback_to_httpx=%s",
                    url,
                    type(exc).__name__,
                    exc,
                    allow_httpx_fallback,
                )
                if not allow_httpx_fallback:
                    raise FrankensurfTransportError(
                        f"frankensurf_fetch failed for {url}: {type(exc).__name__}: {exc}"
                    ) from exc

            if not allow_httpx_fallback:
                return None

        for attempt in range(self.config.max_retries + 1):
            try:
                current_url = url
                for _hop in range(self.config.max_redirects + 1):
                    if not await _is_safe_target_url(current_url):
                        return None
                    async with client.stream("GET", current_url) as resp:
                        if resp.status_code in REDIRECT_STATUS_CODES:
                            location = resp.headers.get("location")
                            if not location:
                                return None
                            current_url = urljoin(current_url, location)
                            break

                        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "text/html").lower():
                            return await self._read_bounded_stream(resp)
                        if resp.status_code in {403, 404, 410, 429}:
                            return None
                        return None
                else:
                    return None
            except httpx.HTTPError:
                if attempt == self.config.max_retries:
                    return None
                await asyncio.sleep(0.4 * (2**attempt))
        return None

    async def fetch_lead_signals(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> list[EnrichmentResult]:
        domain = normalize_domain(lead.domain)
        if not domain:
            return []

        base_url = f"https://{domain}"
        if not await _is_safe_target_url(base_url):
            return []

        candidate_urls = [urljoin(base_url, p) for p in self.config.paths[: self.config.max_pages_per_domain]]
        results_by_index: dict[int, _PageSnapshot] = {}

        headers = {"User-Agent": self.config.user_agent, "Accept": "text/html,application/xhtml+xml"}
        timeout = httpx.Timeout(self.config.timeout_seconds)
        page_semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_pages))

        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as client:
            async def _crawl_one(idx: int, target_url: str) -> None:
                async with page_semaphore:
                    html = await self._fetch_html(client, target_url)
                    if html:
                        results_by_index[idx] = _PageSnapshot(url=target_url, html=html)

            tasks = [
                asyncio.create_task(_crawl_one(idx, target_url))
                for idx, target_url in enumerate(candidate_urls)
            ]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.1, self.config.domain_timeout_seconds),
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        pages = [results_by_index[i] for i in sorted(results_by_index)]
        if not pages:
            return []

        result = parse_pages_to_result(
            domain,
            pages,
            target_titles=policy.target_titles,
            existing_extra=lead.extra,
        )
        return [result]

    async def __call__(self, lead: LeadRecord, policy: EnrichmentPolicy) -> list[EnrichmentResult]:
        return await self.fetch_lead_signals(lead, policy)


__all__ = [
    "CrawlConfig",
    "DEFAULT_PATHS",
    "FrankensurfFetcher",
    "FrankensurfTransportError",
    "WebCrawlerProvider",
    "parse_pages_to_result",
]