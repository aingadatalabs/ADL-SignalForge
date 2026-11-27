"""Stage 1 corporate registry grounding (UK Companies House + OpenCorporates)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..base import (
    ContactRecord,
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    normalize_domain,
)

logger = logging.getLogger(__name__)

COMPANIES_HOUSE_BASE_URL = "https://api.company-information.service.gov.uk"
OPENCORPORATES_BASE_URL = "https://api.opencorporates.com/v0.4"
COMPANIES_HOUSE_MAX_PAGE_SIZE = 100

LEGAL_SUFFIX_PATTERN = re.compile(
    r"\b(limited|ltd|plc|llp|inc|incorporated|llc|corp|corporation|gmbh|co|group|holdings)\b\.?",
    re.IGNORECASE,
)

NAME_SUFFIXES_AND_HONORS: frozenset[str] = frozenset(
    {
        "JR",
        "JR.",
        "SR",
        "SR.",
        "II",
        "III",
        "IV",
        "V",
        "ESQ",
        "ESQ.",
        "CBE",
        "OBE",
        "MBE",
        "KBE",
        "DBE",
        "GBE",
        "PC",
        "QC",
        "KC",
        "FCA",
        "FCCA",
        "ACA",
        "FRS",
        "FRCS",
        "FRCP",
        "PHD",
        "PH.D.",
        "MD",
        "MBA",
        "BSC",
        "MSC",
    }
)

UPPERCASE_ACRONYMS: frozenset[str] = frozenset(
    {
        "UK",
        "US",
        "USA",
        "EU",
        "UAE",
        "AI",
        "IT",
        "API",
        "HR",
        "PR",
        "R&D",
        "TV",
        "VP",
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "CMO",
        "CIO",
        "B2B",
        "B2C",
        "SAAS",
        "IP",
        "VC",
        "PE",
        "FX",
        "EV",
        "ESG",
        "FINTECH",
        "MEDTECH",
        "EDTECH",
    }
)

LOWERCASE_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "via",
    }
)

SURNAME_PARTICLES: frozenset[str] = frozenset(
    {"von", "van", "de", "del", "della", "di", "da", "la", "le", "du", "al"}
)


@dataclass
class RegistriesConfig:
    timeout_seconds: float = 10.0
    max_retries: int = 2
    max_officers: int = 10
    min_match_score: float = 0.65
    prefer_uk_for_co_uk: bool = True
    allow_registered_address_as_location: bool = False
    max_retry_after_seconds: float = 10.0
    user_agent: str = "ADL-SignalForge-Registries/1.0 (+https://aingadatalabs.com)"


def _parse_retry_after_seconds(header_val: str | None, max_wait: float) -> float | None:
    """Parse HTTP Retry-After header (seconds or HTTP-date) clamped to `max_wait`."""
    if not header_val:
        return None
    raw = header_val.strip()
    try:
        seconds = float(raw)
        if seconds >= 0:
            return min(seconds, max_wait)
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        if delta > 0:
            return min(delta, max_wait)
    except (TypeError, ValueError):
        pass
    return None


def _is_uk_domain(domain: str | None) -> bool:
    """Return True if the normalized domain belongs to a UK TLD such as .co.uk or .uk."""
    norm = normalize_domain(domain)
    if not norm:
        return False
    return norm.endswith(".co.uk") or norm.endswith(".uk")


def normalize_company_name_for_match(name: str | None) -> str:
    if not name:
        return ""
    stripped = LEGAL_SUFFIX_PATTERN.sub("", name.lower())
    stripped = re.sub(r"[^a-z0-9]+", " ", stripped).strip()
    return stripped


def _smart_title_token(token: str, *, is_first: bool = False) -> str:
    """Title-case a single token while respecting particles, hyphens, and Mc/Mac prefixes."""
    if not token:
        return ""
    if "-" in token:
        return "-".join(_smart_title_token(part, is_first=is_first) for part in token.split("-"))

    lower = token.lower()
    if not is_first and lower in SURNAME_PARTICLES:
        return lower
    if lower.startswith("mc") and len(lower) > 2:
        return "Mc" + lower[2].upper() + lower[3:]
    if lower.startswith("mac") and len(lower) > 4 and lower[3].isalpha():
        return "Mac" + lower[3].upper() + lower[4:]
    return lower.capitalize()


def _format_name_phrase(phrase: str) -> str:
    tokens = [t for t in phrase.split() if t]
    return " ".join(
        _smart_title_token(tok, is_first=(idx == 0)) for idx, tok in enumerate(tokens)
    )


def _format_suffix_token(raw_suffix: str) -> str:
    cleaned = raw_suffix.strip().strip(",")
    upper = cleaned.upper()
    if upper == "PH.D":
        return "Ph.D."
    if upper in NAME_SUFFIXES_AND_HONORS:
        return upper
    return _format_name_phrase(cleaned)


def format_company_display_name(legal_name: str) -> str:
    """Format an all-caps legal name while preserving acronyms and initialisms."""
    cleaned = re.sub(r"\s+", " ", legal_name).strip()
    if not cleaned:
        return ""
    if not cleaned.isupper():
        return cleaned

    tokens = cleaned.split(" ")
    formatted: list[str] = []
    for idx, tok in enumerate(tokens):
        m = re.match(r"^([^A-Za-z0-9&]*)([A-Za-z0-9&./-]+)([^A-Za-z0-9&]*)$", tok)
        if not m:
            formatted.append(tok.capitalize())
            continue
        prefix, core, suffix = m.group(1), m.group(2), m.group(3)
        core_upper = core.upper()
        core_lower = core.lower()

        if core_upper in UPPERCASE_ACRONYMS:
            rendered_core = core_upper
        elif idx > 0 and core_lower in LOWERCASE_WORDS:
            rendered_core = core_lower
        elif len(core) <= 3 and core.isalpha() and core_lower not in LOWERCASE_WORDS:
            rendered_core = core_upper if len(core) == 2 else _smart_title_token(core, is_first=(idx == 0))
        else:
            rendered_core = _smart_title_token(core, is_first=(idx == 0))

        formatted.append(f"{prefix}{rendered_core}{suffix}")

    return " ".join(formatted)


def format_officer_name(raw_name: str) -> tuple[str, str | None, str | None]:
    """
    Convert Companies House 'SURNAME, First Middle' (including trailing suffixes/honors
    like 'Smith, John Jr.' or 'Doe, Jane, CBE') into 'First Middle Surname [Suffix]'.
    """
    cleaned = re.sub(r"\s+", " ", raw_name).strip()
    if not cleaned:
        return "", None, None

    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        surname_raw = parts[0]
        given_raw = parts[1] if len(parts) > 1 else ""
        extra_parts = parts[2:] if len(parts) > 2 else []

        given_tokens = given_raw.split()
        trailing_suffixes: list[str] = []
        while len(given_tokens) > 1 and given_tokens[-1].upper() in NAME_SUFFIXES_AND_HONORS:
            trailing_suffixes.insert(0, given_tokens.pop())

        given_clean = _format_name_phrase(" ".join(given_tokens))
        surname_clean = _format_name_phrase(surname_raw)

        all_suffixes = [_format_suffix_token(s) for s in (trailing_suffixes + extra_parts)]
        if all_suffixes:
            generational = [s for s in all_suffixes if s in {"JR.", "SR.", "II", "III", "IV", "V", "ESQ."}]
            honors = [s for s in all_suffixes if s not in {"JR.", "SR.", "II", "III", "IV", "V", "ESQ."}]
            base_with_gen = " ".join(p for p in [given_clean, surname_clean, *generational] if p)
            full = f"{base_with_gen}, {', '.join(honors)}" if honors else base_with_gen
        else:
            full = " ".join(p for p in [given_clean, surname_clean] if p)

        first_token = given_clean.split()[0] if given_clean else None
        last_parts = [p for p in [surname_clean, *all_suffixes] if p]
        last_token = " ".join(last_parts) if last_parts else None
        return full.strip(), first_token, last_token

    titled = _format_name_phrase(cleaned)
    tokens = titled.split()
    first = tokens[0] if tokens else None
    last = " ".join(tokens[1:]) if len(tokens) > 1 else None
    return titled, first, last


def _format_ch_address(addr: dict[str, Any] | None) -> str | None:
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get("address_line_1"),
        addr.get("locality"),
        addr.get("region"),
        addr.get("postal_code"),
        addr.get("country"),
    ]
    joined = ", ".join(str(p).strip() for p in parts if p)
    return joined or None


def parse_companies_house_payload(
    *,
    domain: str | None,
    profile: dict[str, Any],
    officers_payload: dict[str, Any] | None = None,
    target_titles: list[str] | None = None,
    existing_location: str | None = None,
    allow_registered_address_as_location: bool = False,
) -> EnrichmentResult:
    """Turn Companies House profile + officers JSON into a normalized EnrichmentResult."""
    company_number = str(profile.get("company_number") or "").strip()
    legal_name = str(profile.get("company_name") or "").strip() or None
    company_status = str(profile.get("company_status") or "").strip() or None
    company_type = str(profile.get("type") or "").strip() or None
    date_of_creation = str(profile.get("date_of_creation") or "").strip() or None
    sic_codes = [str(s) for s in (profile.get("sic_codes") or []) if s]
    registered_address = _format_ch_address(profile.get("registered_office_address"))

    title_filters = [t.strip().lower() for t in (target_titles or []) if t.strip()]
    contacts: list[ContactRecord] = []
    active_officers_summary: list[dict[str, Any]] = []

    items = (officers_payload or {}).get("items") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("resigned_on"):
            continue
        raw_name = str(item.get("name") or "").strip()
        if not raw_name:
            continue

        officer_role = str(item.get("officer_role") or "director").replace("-", " ").title()
        full_name, first_name, last_name = format_officer_name(raw_name)

        active_officers_summary.append(
            {
                "name": full_name,
                "role": officer_role,
                "appointed_on": item.get("appointed_on"),
            }
        )

        if title_filters and not any(tf in officer_role.lower() for tf in title_filters):
            continue

        contacts.append(
            ContactRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                title=officer_role,
                verified_status=VerificationStatus.UNVERIFIED,
                confidence=0.82,
                source="registries:companies_house",
            )
        )

    registry_url = (
        f"https://find-and-update.company-information.service.gov.uk/company/{company_number}"
        if company_number
        else None
    )

    registry_data = {
        "registry": "uk_companies_house",
        "company_number": company_number,
        "legal_name": legal_name,
        "company_status": company_status,
        "company_type": company_type,
        "date_of_creation": date_of_creation,
        "sic_codes": sic_codes,
        "registered_address": registered_address,
        "active_officers": active_officers_summary,
        "registry_url": registry_url,
    }

    company_patch: dict[str, Any] = {
        "extra": {"registry_intelligence": registry_data},
    }
    if domain:
        company_patch["domain"] = normalize_domain(domain)
    if legal_name:
        company_patch["company_name"] = format_company_display_name(legal_name)
    if (
        registered_address
        and allow_registered_address_as_location
        and not (existing_location and existing_location.strip())
    ):
        company_patch["location"] = registered_address

    confidence = 0.88 if company_status == "active" else 0.72

    return EnrichmentResult(
        source="registries:companies_house",
        stage="stage_1",
        confidence=confidence,
        verified_status=VerificationStatus.UNVERIFIED,
        credits_used=0.0,
        company_patch=company_patch,
        contacts=contacts,
        raw_payload=registry_data,
    )


def parse_opencorporates_company(
    *,
    domain: str | None,
    company_obj: dict[str, Any],
    existing_location: str | None = None,
    allow_registered_address_as_location: bool = False,
) -> EnrichmentResult:
    """Turn an OpenCorporates company record into a normalized EnrichmentResult."""
    legal_name = str(company_obj.get("name") or "").strip() or None
    company_number = str(company_obj.get("company_number") or "").strip() or None
    jurisdiction = str(company_obj.get("jurisdiction_code") or "").strip() or None
    status = str(company_obj.get("current_status") or "").strip() or None
    incorporation_date = str(company_obj.get("incorporation_date") or "").strip() or None
    registered_address = str(company_obj.get("registered_address_in_full") or "").strip() or None
    oc_url = str(company_obj.get("opencorporates_url") or "").strip() or None

    registry_data = {
        "registry": "opencorporates",
        "company_number": company_number,
        "jurisdiction_code": jurisdiction,
        "legal_name": legal_name,
        "company_status": status,
        "date_of_creation": incorporation_date,
        "registered_address": registered_address,
        "registry_url": oc_url,
    }

    company_patch: dict[str, Any] = {
        "extra": {"registry_intelligence": registry_data},
    }
    if domain:
        company_patch["domain"] = normalize_domain(domain)
    if legal_name:
        company_patch["company_name"] = format_company_display_name(legal_name)
    if (
        registered_address
        and allow_registered_address_as_location
        and not (existing_location and existing_location.strip())
    ):
        company_patch["location"] = registered_address

    return EnrichmentResult(
        source="registries:opencorporates",
        stage="stage_1",
        confidence=0.78,
        verified_status=VerificationStatus.UNVERIFIED,
        credits_used=0.0,
        company_patch=company_patch,
        raw_payload=registry_data,
    )


class RegistriesProvider:
    """Stage 1 public registry fetcher (Companies House + OpenCorporates)."""

    def __init__(
        self,
        config: RegistriesConfig | None = None,
        *,
        companies_house_api_key: str | None = None,
        opencorporates_api_key: str | None = None,
    ) -> None:
        self.config = config or RegistriesConfig()
        self.ch_api_key = (
            companies_house_api_key
            or os.environ.get("COMPANIES_HOUSE_API_KEY")
            or os.environ.get("COH_API_KEY")
            or ""
        ).strip()
        self.oc_api_key = (
            opencorporates_api_key
            or os.environ.get("OPENCORPORATES_API_KEY")
            or ""
        ).strip()
        self.last_status_notes: list[str] = []

    def _candidate_query_name(self, lead: LeadRecord) -> str:
        if lead.company_name and lead.company_name.strip():
            return lead.company_name.strip()
        norm_dom = normalize_domain(lead.domain)
        if norm_dom:
            return norm_dom.split(".", 1)[0]
        return ""

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        source_label: str = "registry",
        params: dict[str, Any] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
    ) -> dict[str, Any] | None:
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.get(url, params=params, auth=auth)
                if resp.status_code == 200:
                    data = resp.json()
                    return data if isinstance(data, dict) else None

                if resp.status_code in {401, 403}:
                    note = f"{source_label}_auth_or_plan_rejected_http_{resp.status_code}"
                    logger.warning(
                        "%s (%s) for url=%s; check API token or endpoint tier permissions",
                        note,
                        source_label,
                        url,
                    )
                    if note not in self.last_status_notes:
                        self.last_status_notes.append(note)
                    return None

                if resp.status_code == 404:
                    return None

                if resp.status_code == 429:
                    note = f"{source_label}_rate_limited_http_429"
                    if note not in self.last_status_notes:
                        self.last_status_notes.append(note)
                    if attempt >= self.config.max_retries:
                        logger.warning(
                            "%s exhausted max_retries=%d for url=%s",
                            note,
                            self.config.max_retries,
                            url,
                        )
                        return None
                    retry_after = _parse_retry_after_seconds(
                        resp.headers.get("Retry-After"),
                        self.config.max_retry_after_seconds,
                    )
                    sleep_s = retry_after if retry_after is not None else min(
                        self.config.max_retry_after_seconds, 1.0 * (2**attempt)
                    )
                    logger.info(
                        "%s on attempt %d/%d for url=%s; sleeping %.2fs before retry",
                        note,
                        attempt + 1,
                        self.config.max_retries + 1,
                        url,
                        sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    continue

                if attempt >= self.config.max_retries:
                    return None
                await asyncio.sleep(0.5 * (2**attempt))
            except httpx.HTTPError as exc:
                logger.debug(
                    "%s HTTP error on attempt %d/%d for url=%s: %s",
                    source_label,
                    attempt + 1,
                    self.config.max_retries + 1,
                    url,
                    exc,
                )
                if attempt >= self.config.max_retries:
                    return None
                await asyncio.sleep(0.5 * (2**attempt))
        return None

    async def _fetch_companies_house_officers(
        self,
        client: httpx.AsyncClient,
        company_number: str,
        auth: tuple[str, str],
    ) -> dict[str, Any] | None:
        """Fetch officers with safe per-page clamping [1, 100] and start_index pagination."""
        max_officers = self.config.max_officers
        if max_officers <= 0:
            return {"items": [], "total_results": 0}

        collected_items: list[dict[str, Any]] = []
        start_index = 0
        total_results: int | None = None

        while len(collected_items) < max_officers:
            remaining = max_officers - len(collected_items)
            page_size = max(1, min(remaining, COMPANIES_HOUSE_MAX_PAGE_SIZE))
            page_payload = await self._get_json(
                client,
                f"{COMPANIES_HOUSE_BASE_URL}/company/{company_number}/officers",
                source_label="companies_house",
                params={"items_per_page": page_size, "start_index": start_index},
                auth=auth,
            )
            if not page_payload:
                if not collected_items:
                    return None
                break

            raw_items = page_payload.get("items") or []
            if not isinstance(raw_items, list) or not raw_items:
                break

            if total_results is None and isinstance(page_payload.get("total_results"), int):
                total_results = page_payload["total_results"]

            for item in raw_items:
                if isinstance(item, dict):
                    collected_items.append(item)
                if len(collected_items) >= max_officers:
                    break

            start_index += len(raw_items)
            if len(raw_items) < page_size:
                break
            if total_results is not None and start_index >= total_results:
                break

        return {
            "items": collected_items,
            "total_results": total_results if total_results is not None else len(collected_items),
        }

    async def _lookup_companies_house(
        self,
        client: httpx.AsyncClient,
        query_name: str,
        domain: str | None,
        target_titles: list[str] | None,
        existing_location: str | None = None,
    ) -> EnrichmentResult | None:
        if not self.ch_api_key:
            return None

        auth = (self.ch_api_key, "")
        search_data = await self._get_json(
            client,
            f"{COMPANIES_HOUSE_BASE_URL}/search/companies",
            source_label="companies_house",
            params={"q": query_name, "items_per_page": 5},
            auth=auth,
        )
        items = (search_data or {}).get("items") or []
        if not items:
            return None

        best_item: dict[str, Any] | None = None
        best_score = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            is_active = str(item.get("company_status") or "").lower() == "active"
            score = _compute_company_match_score(
                query_name,
                item.get("title"),
                domain=domain,
                is_active=is_active,
                prefer_uk=self.config.prefer_uk_for_co_uk,
                jurisdiction_code="gb",
            )
            if score > best_score:
                best_score = score
                best_item = item

        if best_item is None or best_score < self.config.min_match_score:
            logger.debug(
                "Companies House rejected candidates for query=%r (best_score=%.2f < min=%.2f)",
                query_name,
                best_score,
                self.config.min_match_score,
            )
            return None

        company_number = str(best_item.get("company_number") or "").strip()
        if not company_number:
            return None

        profile_task = self._get_json(
            client,
            f"{COMPANIES_HOUSE_BASE_URL}/company/{company_number}",
            source_label="companies_house",
            auth=auth,
        )
        officers_task = self._fetch_companies_house_officers(client, company_number, auth)
        profile, officers = await asyncio.gather(profile_task, officers_task)
        if not profile:
            return None

        return parse_companies_house_payload(
            domain=domain,
            profile=profile,
            officers_payload=officers,
            target_titles=target_titles,
            existing_location=existing_location,
            allow_registered_address_as_location=self.config.allow_registered_address_as_location,
        )

    async def _lookup_opencorporates(
        self,
        client: httpx.AsyncClient,
        query_name: str,
        domain: str | None,
        existing_location: str | None = None,
    ) -> EnrichmentResult | None:
        if not self.oc_api_key:
            logger.debug(
                "OpenCorporates API token not configured; unauthenticated v0.4 queries may be rate-limited or rejected"
            )

        params: dict[str, Any] = {"q": query_name, "per_page": 5}
        if self.oc_api_key:
            params["api_token"] = self.oc_api_key
        if self.config.prefer_uk_for_co_uk and _is_uk_domain(domain):
            params["jurisdiction_code"] = "gb"

        data = await self._get_json(
            client,
            f"{OPENCORPORATES_BASE_URL}/companies/search",
            source_label="opencorporates",
            params=params,
        )
        results_obj = (data or {}).get("results")
        companies = results_obj.get("companies") if isinstance(results_obj, dict) else None
        if not isinstance(companies, list) or not companies:
            return None

        best_company: dict[str, Any] | None = None
        best_score = 0.0
        for entry in companies:
            comp = entry.get("company") if isinstance(entry, dict) and "company" in entry else entry
            if not isinstance(comp, dict):
                continue
            is_active = not bool(comp.get("inactive"))
            jurisdiction = str(comp.get("jurisdiction_code") or "").strip() or None
            score = _compute_company_match_score(
                query_name,
                comp.get("name"),
                domain=domain,
                is_active=is_active,
                prefer_uk=self.config.prefer_uk_for_co_uk,
                jurisdiction_code=jurisdiction,
            )
            if score > best_score:
                best_score = score
                best_company = comp

        if best_company is None or best_score < self.config.min_match_score:
            logger.debug(
                "OpenCorporates rejected candidates for query=%r (best_score=%.2f < min=%.2f)",
                query_name,
                best_score,
                self.config.min_match_score,
            )
            return None

        return parse_opencorporates_company(
            domain=domain,
            company_obj=best_company,
            existing_location=existing_location,
            allow_registered_address_as_location=self.config.allow_registered_address_as_location,
        )

    async def fetch_lead_signals(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> list[EnrichmentResult]:
        self.last_status_notes = []
        query_name = self._candidate_query_name(lead)
        if not query_name:
            return []

        domain = normalize_domain(lead.domain)
        headers = {"User-Agent": self.config.user_agent, "Accept": "application/json"}
        timeout = httpx.Timeout(self.config.timeout_seconds)

        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            ch_res = await self._lookup_companies_house(
                client,
                query_name,
                domain,
                policy.target_titles,
                existing_location=lead.location,
            )
            if ch_res is not None:
                return [ch_res]

            oc_res = await self._lookup_opencorporates(
                client,
                query_name,
                domain,
                existing_location=lead.location,
            )
            if oc_res is not None:
                return [oc_res]

        return []

    async def __call__(self, lead: LeadRecord, policy: EnrichmentPolicy) -> list[EnrichmentResult]:
        return await self.fetch_lead_signals(lead, policy)


__all__ = [
    "COMPANIES_HOUSE_BASE_URL",
    "COMPANIES_HOUSE_MAX_PAGE_SIZE",
    "OPENCORPORATES_BASE_URL",
    "RegistriesConfig",
    "RegistriesProvider",
    "format_company_display_name",
    "format_officer_name",
    "normalize_company_name_for_match",
    "parse_companies_house_payload",
    "parse_opencorporates_company",
]