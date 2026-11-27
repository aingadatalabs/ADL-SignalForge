"""Stage 2 credit-gated API adapter mapping .env providers to normalized EnrichmentResult."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..base import (
    ContactRecord,
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    normalize_domain,
    normalize_email,
)

ICYPEAS_PENDING_STATUSES: frozenset[str] = frozenset(
    {"", "NONE", "PENDING", "QUEUED", "SCHEDULED", "IN_PROGRESS", "PROCESSING"}
)
ICYPEAS_TERMINAL_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "FAILED",
        "ERROR",
        "ERR",
        "NOT_FOUND",
        "NO_RESULT",
        "CANCELLED",
        "CANCELED",
        "EXPIRED",
        "TIMEOUT",
        "TIMED_OUT",
    }
)
ICYPEAS_COMPLETED_STATUSES: frozenset[str] = frozenset(
    {"FOUND", "COMPLETED", "COMPLETE", "DONE", "FINISHED", "SUCCESS", "SUCCEEDED"}
)
_NAME_WHITESPACE_RE = re.compile(r"\s+")
_RANGE_NUMBERS_RE = re.compile(r"(\d[\d,]*)")


class ProviderHttpError(RuntimeError):
    """Structured error representing a non-2xx HTTP response from a Stage 2 provider."""

    def __init__(
        self,
        *,
        provider: str,
        endpoint: str,
        status_code: int,
        category: str,
        message: str,
        retry_after_seconds: float | None = None,
        response_excerpt: str = "",
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.status_code = status_code
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.response_excerpt = response_excerpt
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "category": self.category,
            "retry_after_seconds": self.retry_after_seconds,
            "message": str(self),
            "response_excerpt": self.response_excerpt,
        }


@dataclass
class Stage2ApiConfig:
    timeout_seconds: float = 15.0
    icypeas_poll_interval_seconds: float = 3.0
    icypeas_max_polls: int = 5
    icypeas_max_total_seconds: float | None = None
    require_person_for_apollo_match: bool = True
    raise_provider_http_errors: bool = False
    valid_email_credits: float = 1.0
    unverified_email_credits: float = 0.5
    company_enrichment_credits: float = 0.25
    no_email_contact_credits: float = 0.0
    user_agent: str = "ADL-SignalForge-Stage2/1.0 (+https://aingadatalabs.com)"
    last_provider_errors: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    cleaned = raw.strip()
    try:
        val = float(cleaned)
        return val if val >= 0 else None
    except ValueError:
        return None


def _classify_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth"
    if status_code == 402:
        return "credits"
    if status_code == 429:
        return "rate_limit"
    if 500 <= status_code < 600:
        return "server_error"
    return "client_error"


def _build_provider_http_error(
    provider: str,
    endpoint: str,
    resp: httpx.Response,
) -> ProviderHttpError:
    status = resp.status_code
    category = _classify_http_status(status)
    retry_after = _parse_retry_after(resp.headers)
    excerpt = ""
    try:
        excerpt = (resp.text or "").strip()[:300]
    except Exception:
        excerpt = ""

    detail = f" ({excerpt})" if excerpt else ""
    retry_info = f" [retry_after={retry_after}s]" if retry_after is not None else ""
    msg = f"{provider} {endpoint} returned HTTP {status} [{category}]{retry_info}{detail}"
    return ProviderHttpError(
        provider=provider,
        endpoint=endpoint,
        status_code=status,
        category=category,
        message=msg,
        retry_after_seconds=retry_after,
        response_excerpt=excerpt,
    )


def _safe_parse_json_dict(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        parsed = resp.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_name_part(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _NAME_WHITESPACE_RE.sub(" ", value).strip(" \t\r\n,")
    return cleaned or None


def _bucket_employee_count(count: int) -> str:
    if count <= 0:
        return "0"
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
    if count <= 10000:
        return "5001-10000"
    return "10001+"


def _normalize_company_size(raw_size: Any) -> str | None:
    """Normalize numeric headcount or size strings into consistent range buckets."""
    if raw_size is None or isinstance(raw_size, bool):
        return None

    if isinstance(raw_size, (int, float)):
        return _bucket_employee_count(int(raw_size))

    text = str(raw_size).strip()
    if not text:
        return None

    range_match = re.fullmatch(r"(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)", text)
    if range_match:
        low = int(range_match.group(1).replace(",", ""))
        high = int(range_match.group(2).replace(",", ""))
        if low <= high:
            return _bucket_employee_count(high)

    plus_match = re.fullmatch(r"(\d[\d,]*)\s*\+", text)
    if plus_match:
        return _bucket_employee_count(int(plus_match.group(1).replace(",", "")) + 1)

    nums = _RANGE_NUMBERS_RE.findall(text)
    if nums:
        try:
            parsed_int = int(nums[-1].replace(",", ""))
            return _bucket_employee_count(parsed_int)
        except ValueError:
            pass

    return text


def _extract_prospeo_email(person: dict[str, Any]) -> tuple[str | None, str]:
    """Extract normalized email and verification status from flexible Prospeo person shapes."""
    raw_email = person.get("email")
    email_obj = raw_email if isinstance(raw_email, dict) else {}

    candidate_email = (
        email_obj.get("email")
        or email_obj.get("address")
        or email_obj.get("value")
        or email_obj.get("email_address")
        or (raw_email if isinstance(raw_email, str) else None)
        or person.get("email_address")
    )
    email = normalize_email(candidate_email)

    status_raw = str(
        email_obj.get("status")
        or email_obj.get("verification_status")
        or person.get("email_status")
        or "VERIFIED"
    ).upper()
    return email, status_raw


def _extract_provider_reported_credits(*payloads: Any) -> float | None:
    """Extract an explicit numeric credit count from provider response payloads if present."""
    credit_keys = ("credits_used", "credit_cost", "credits_consumed", "credits")
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in credit_keys:
            val = payload.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                return float(val)
        meta = payload.get("meta")
        if isinstance(meta, dict):
            for key in credit_keys:
                val = meta.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                    return float(val)
    return None


def _resolve_credits_used(
    config: Stage2ApiConfig,
    *,
    outcome: str,
    ver_status: VerificationStatus,
    has_email: bool,
    payloads: tuple[Any, ...] = (),
) -> tuple[float, float | None]:
    """
    Compute billable credit usage for an enrichment outcome.
    Returns `(credits_to_charge, provider_reported_credits)`.
    """
    reported = _extract_provider_reported_credits(*payloads)
    if reported is not None:
        if outcome in {"person_email", "company"} and not has_email and reported == 0.0:
            return 0.0, reported
        return round(reported, 4), reported

    if outcome == "person_email":
        if not has_email:
            return max(0.0, float(config.no_email_contact_credits)), None
        if ver_status == VerificationStatus.VALID:
            return max(0.0, float(config.valid_email_credits)), None
        return max(0.0, float(config.unverified_email_credits)), None

    if outcome == "company":
        return max(0.0, float(config.company_enrichment_credits)), None

    return 0.0, None


def _attach_credit_metadata(
    payload: dict[str, Any],
    *,
    outcome: str,
    credits_used: float,
    provider_reported_credits: float | None,
) -> dict[str, Any]:
    enriched_payload = dict(payload)
    enriched_payload["_credit_accounting"] = {
        "outcome": outcome,
        "credits_used": credits_used,
        "provider_reported_credits": provider_reported_credits,
    }
    return enriched_payload


def _pick_target_contact(lead: LeadRecord, policy: EnrichmentPolicy) -> ContactRecord | None:
    """Select the highest-priority contact missing a verified email."""
    if not lead.contacts:
        return None

    title_filters = [t.strip().lower() for t in policy.target_titles if t.strip()]

    def _score(c: ContactRecord) -> tuple[int, int, float]:
        needs_email = int(not c.email or c.verified_status != VerificationStatus.VALID)
        matches_title = int(
            bool(title_filters and c.title and any(tf in c.title.lower() for tf in title_filters))
        )
        return (needs_email, matches_title, c.confidence)

    ranked = sorted(lead.contacts, key=_score, reverse=True)
    best = ranked[0]
    if best.email and best.verified_status == VerificationStatus.VALID:
        return None
    return best


def _split_name(contact: ContactRecord | None) -> tuple[str | None, str | None, str | None]:
    """
    Normalize contact name components cleanly while preserving multi-part surnames
    (e.g. 'Jane van der Berg') and single-word/mononym names (e.g. 'Madonna').
    """
    if contact is None:
        return None, None, None
    full = _clean_name_part(contact.full_name)
    first = _clean_name_part(contact.first_name)
    last = _clean_name_part(contact.last_name)

    if full and (not first or not last):
        parts = full.split(" ")
        if len(parts) == 1:
            first = first or parts[0]
        elif len(parts) > 1:
            first = first or parts[0]
            last = last or " ".join(parts[1:])
    elif not full and (first or last):
        full = " ".join(p for p in (first, last) if p)

    return full, first, last


def _has_person_identifier(
    contact: ContactRecord | None,
    full_name: str | None,
    first_name: str | None,
    last_name: str | None,
) -> bool:
    """Return True if a usable person name or LinkedIn profile URL is available."""
    linkedin_url = ((contact.linkedin_url if contact else None) or "").strip()
    return bool(linkedin_url or full_name or first_name or last_name)


def _extract_icypeas_email_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    """Extract top email payload from an Icypeas result item if present."""
    results_block = item.get("results")
    if isinstance(results_block, dict):
        emails = results_block.get("emails")
        if isinstance(emails, list):
            for entry in emails:
                if isinstance(entry, dict) and normalize_email(entry.get("email")):
                    return entry
    if normalize_email(item.get("email")):
        return item
    return None


class Stage2ApiAdapter:
    """Dispatches Stage 2 waterfall calls for configured .env API keys."""

    def __init__(self, config: Stage2ApiConfig | None = None) -> None:
        self.config = config or Stage2ApiConfig()

    def _record_and_handle_http_error(
        self,
        provider: str,
        endpoint: str,
        resp: httpx.Response,
    ) -> None:
        err = _build_provider_http_error(provider, endpoint, resp)
        self.config.last_provider_errors[provider] = err.to_dict()
        if self.config.raise_provider_http_errors:
            raise err

    def _ensure_ok_response(
        self,
        provider: str,
        endpoint: str,
        resp: httpx.Response,
    ) -> bool:
        """
        Validate a 2xx response without leaking raw httpx.HTTPStatusError.
        Returns True if 2xx, or handles non-2xx cleanly (recording structured error
        and optionally raising ProviderHttpError).
        """
        if 200 <= resp.status_code < 300:
            self.config.last_provider_errors.pop(provider, None)
            return True
        self._record_and_handle_http_error(provider, endpoint, resp)
        return False

    def _icypeas_budget_seconds(self) -> float:
        if self.config.icypeas_max_total_seconds is not None:
            return max(0.5, float(self.config.icypeas_max_total_seconds))
        planned_poll_sleep = max(0, self.config.icypeas_max_polls) * max(
            0.0, self.config.icypeas_poll_interval_seconds
        )
        return max(
            self.config.timeout_seconds,
            planned_poll_sleep + min(self.config.timeout_seconds, 5.0),
        )

    async def _enrich_icypeas(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        lead: LeadRecord,
        contact: ContactRecord | None,
    ) -> EnrichmentResult | None:
        domain = normalize_domain(lead.domain)
        full_name, first_name, last_name = _split_name(contact)
        if not domain or not (first_name or last_name or full_name):
            return None

        deadline = time.monotonic() + self._icypeas_budget_seconds()
        headers = {"Authorization": api_key, "Content-Type": "application/json"}

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        search_payload: dict[str, Any] = {"domainOrCompany": domain}
        if first_name:
            search_payload["firstname"] = first_name
        if last_name:
            search_payload["lastname"] = last_name
        if full_name and not last_name:
            search_payload["fullname"] = full_name

        start_resp = await client.post(
            "https://app.icypeas.com/api/email-search",
            headers=headers,
            json=search_payload,
            timeout=min(self.config.timeout_seconds, remaining),
        )
        if not self._ensure_ok_response("icypeas", "/api/email-search", start_resp):
            return None

        start_data = _safe_parse_json_dict(start_resp)
        if not start_data or not start_data.get("success"):
            return None

        search_id = ((start_data.get("item") or {}).get("_id") or "").strip()
        if not search_id:
            return None

        found_item: dict[str, Any] | None = None
        last_read_data: dict[str, Any] = {}
        poll_interval = max(0.0, self.config.icypeas_poll_interval_seconds)

        for _ in range(max(0, self.config.icypeas_max_polls)):
            remaining_before_sleep = deadline - time.monotonic()
            if remaining_before_sleep <= 0:
                break
            sleep_s = min(poll_interval, remaining_before_sleep)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

            remaining_for_req = deadline - time.monotonic()
            if remaining_for_req <= 0:
                break

            read_resp = await client.post(
                "https://app.icypeas.com/api/bulk-single-searchs/read",
                headers=headers,
                json={"id": search_id},
                timeout=min(self.config.timeout_seconds, remaining_for_req),
            )
            if not self._ensure_ok_response("icypeas", "/api/bulk-single-searchs/read", read_resp):
                return None

            read_data = _safe_parse_json_dict(read_resp)
            if not read_data:
                continue
            last_read_data = read_data

            items = read_data.get("items") or []
            item = (
                items[0]
                if items and isinstance(items[0], dict)
                else (read_data.get("item") if isinstance(read_data.get("item"), dict) else {})
            )
            if not isinstance(item, dict):
                continue

            status = str(item.get("status") or "").strip().upper()
            if status in ICYPEAS_PENDING_STATUSES:
                continue
            if status in ICYPEAS_TERMINAL_FAILURE_STATUSES:
                return None

            candidate = _extract_icypeas_email_candidate(item)
            if status in ICYPEAS_COMPLETED_STATUSES or candidate is not None:
                found_item = item
                break

            return None

        if not found_item:
            return None

        top_email_obj = _extract_icypeas_email_candidate(found_item)
        if not top_email_obj:
            return None

        email = normalize_email(top_email_obj.get("email"))
        if not email:
            return None

        certainty = str(top_email_obj.get("certainty") or "").lower()
        ver_status = (
            VerificationStatus.VALID
            if certainty in {"ultra_sure", "sure", "valid"}
            else VerificationStatus.UNVERIFIED
        )

        credits_used, reported_credits = _resolve_credits_used(
            self.config,
            outcome="person_email",
            ver_status=ver_status,
            has_email=True,
            payloads=(found_item, last_read_data, start_data),
        )

        enriched_contact = ContactRecord(
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            title=contact.title if contact else None,
            email=email,
            linkedin_url=contact.linkedin_url if contact else None,
            verified_status=ver_status,
            confidence=0.90 if ver_status == VerificationStatus.VALID else 0.72,
            source="icypeas",
        )

        return EnrichmentResult(
            source="icypeas",
            stage="stage_2",
            confidence=enriched_contact.confidence,
            verified_status=ver_status,
            credits_used=credits_used,
            company_patch={"domain": domain},
            contacts=[enriched_contact],
            raw_payload=_attach_credit_metadata(
                found_item,
                outcome="person_email",
                credits_used=credits_used,
                provider_reported_credits=reported_credits,
            ),
        )

    async def _enrich_findymail(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        lead: LeadRecord,
        contact: ContactRecord | None,
    ) -> EnrichmentResult | None:
        domain = normalize_domain(lead.domain)
        full_name, first_name, last_name = _split_name(contact)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if domain and full_name:
            resp = await client.post(
                "https://app.findymail.com/api/search/name",
                headers=headers,
                json={"name": full_name, "domain": domain},
            )
            if not self._ensure_ok_response("findymail", "/api/search/name", resp):
                return None

            data = _safe_parse_json_dict(resp)
            c_obj = data.get("contact") if isinstance(data, dict) else None
            email = normalize_email((c_obj or {}).get("email"))
            if email:
                credits_used, reported_credits = _resolve_credits_used(
                    self.config,
                    outcome="person_email",
                    ver_status=VerificationStatus.VALID,
                    has_email=True,
                    payloads=(data, c_obj),
                )
                enriched_contact = ContactRecord(
                    full_name=(c_obj or {}).get("name") or full_name,
                    first_name=first_name,
                    last_name=last_name,
                    title=contact.title if contact else None,
                    email=email,
                    linkedin_url=contact.linkedin_url if contact else None,
                    verified_status=VerificationStatus.VALID,
                    confidence=0.92,
                    source="findymail",
                )
                return EnrichmentResult(
                    source="findymail",
                    stage="stage_2",
                    confidence=0.92,
                    verified_status=VerificationStatus.VALID,
                    credits_used=credits_used,
                    company_patch={"domain": domain},
                    contacts=[enriched_contact],
                    raw_payload=_attach_credit_metadata(
                        data or {},
                        outcome="person_email",
                        credits_used=credits_used,
                        provider_reported_credits=reported_credits,
                    ),
                )

        if domain:
            comp_resp = await client.post(
                "https://app.findymail.com/api/search/company",
                headers=headers,
                json={"domain": domain},
            )
            if not self._ensure_ok_response("findymail", "/api/search/company", comp_resp):
                return None

            comp_data = _safe_parse_json_dict(comp_resp)
            if comp_data and (comp_data.get("name") or comp_data.get("industry")):
                patch: dict[str, Any] = {"domain": domain}
                if comp_data.get("name"):
                    patch["company_name"] = comp_data["name"]
                if comp_data.get("industry"):
                    patch["industry"] = comp_data["industry"]
                if comp_data.get("company_size"):
                    patch["company_size"] = _normalize_company_size(comp_data["company_size"])

                credits_used, reported_credits = _resolve_credits_used(
                    self.config,
                    outcome="company",
                    ver_status=VerificationStatus.UNVERIFIED,
                    has_email=False,
                    payloads=(comp_data,),
                )
                return EnrichmentResult(
                    source="findymail",
                    stage="stage_2",
                    confidence=0.82,
                    verified_status=VerificationStatus.UNVERIFIED,
                    credits_used=credits_used,
                    company_patch=patch,
                    raw_payload=_attach_credit_metadata(
                        comp_data,
                        outcome="company",
                        credits_used=credits_used,
                        provider_reported_credits=reported_credits,
                    ),
                )

        return None

    async def _enrich_prospeo(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        lead: LeadRecord,
        contact: ContactRecord | None,
    ) -> EnrichmentResult | None:
        domain = normalize_domain(lead.domain)
        full_name, first_name, last_name = _split_name(contact)
        linkedin_url = ((contact.linkedin_url if contact else None) or "").strip() or None

        if not (linkedin_url or full_name):
            return None

        person_data: dict[str, Any] = {}
        if linkedin_url:
            person_data["linkedin_url"] = linkedin_url
        if full_name:
            person_data["full_name"] = full_name
        if domain:
            person_data["company_website"] = domain
        elif lead.company_name:
            person_data["company_name"] = lead.company_name

        resp = await client.post(
            "https://api.prospeo.io/enrich-person",
            headers={"X-KEY": api_key, "Content-Type": "application/json"},
            json={"only_verified_email": True, "data": person_data},
        )
        if not self._ensure_ok_response("prospeo", "/enrich-person", resp):
            return None

        data = _safe_parse_json_dict(resp)
        if not data or data.get("error"):
            return None

        person = data.get("person") if isinstance(data.get("person"), dict) else data
        email, status_raw = _extract_prospeo_email(person)
        if not email:
            return None

        ver_status = (
            VerificationStatus.VALID
            if status_raw in {"VERIFIED", "VALID"}
            else VerificationStatus.UNVERIFIED
        )

        credits_used, reported_credits = _resolve_credits_used(
            self.config,
            outcome="person_email",
            ver_status=ver_status,
            has_email=True,
            payloads=(data, person),
        )

        enriched_contact = ContactRecord(
            full_name=person.get("full_name") or full_name,
            first_name=person.get("first_name") or first_name,
            last_name=person.get("last_name") or last_name,
            title=person.get("job_title") or (contact.title if contact else None),
            email=email,
            linkedin_url=person.get("linkedin_url") or linkedin_url,
            verified_status=ver_status,
            confidence=0.91 if ver_status == VerificationStatus.VALID else 0.74,
            source="prospeo",
        )

        return EnrichmentResult(
            source="prospeo",
            stage="stage_2",
            confidence=enriched_contact.confidence,
            verified_status=ver_status,
            credits_used=credits_used,
            company_patch={"domain": domain} if domain else {},
            contacts=[enriched_contact],
            raw_payload=_attach_credit_metadata(
                data,
                outcome="person_email",
                credits_used=credits_used,
                provider_reported_credits=reported_credits,
            ),
        )

    async def _enrich_apollo(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        lead: LeadRecord,
        contact: ContactRecord | None,
    ) -> EnrichmentResult | None:
        domain = normalize_domain(lead.domain)
        full_name, first_name, last_name = _split_name(contact)
        linkedin_url = ((contact.linkedin_url if contact else None) or "").strip() or None
        has_person = _has_person_identifier(contact, full_name, first_name, last_name)

        if self.config.require_person_for_apollo_match and not has_person:
            return None

        payload: dict[str, Any] = {
            "reveal_personal_emails": False,
            "reveal_phone_number": False,
        }
        if domain:
            payload["domain"] = domain
        if lead.company_name:
            payload["organization_name"] = lead.company_name
        if full_name:
            payload["name"] = full_name
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url

        if len(payload) <= 2:
            return None

        resp = await client.post(
            "https://api.apollo.io/api/v1/people/match",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
            json=payload,
        )
        if not self._ensure_ok_response("apollo", "/api/v1/people/match", resp):
            return None

        data = _safe_parse_json_dict(resp)
        person = data.get("person") if isinstance(data, dict) else None
        if not isinstance(person, dict):
            return None

        email = normalize_email(person.get("email"))
        email_status = str(person.get("email_status") or "").lower()
        ver_status = (
            VerificationStatus.VALID
            if email and email_status == "verified"
            else (
                VerificationStatus.CATCH_ALL
                if email and email_status == "likely to engage"
                else VerificationStatus.UNVERIFIED
            )
        )

        org = person.get("organization") if isinstance(person.get("organization"), dict) else {}
        company_patch: dict[str, Any] = {}
        if domain:
            company_patch["domain"] = domain
        if org.get("name"):
            company_patch["company_name"] = org["name"]
        if org.get("industry"):
            company_patch["industry"] = org["industry"]
        raw_emp_count = org.get("estimated_num_employees")
        normalized_size = _normalize_company_size(raw_emp_count)
        if normalized_size:
            company_patch["company_size"] = normalized_size

        contacts: list[ContactRecord] = []
        if email or person.get("name"):
            contacts.append(
                ContactRecord(
                    full_name=person.get("name") or full_name,
                    first_name=person.get("first_name") or first_name,
                    last_name=person.get("last_name") or last_name,
                    title=person.get("title") or (contact.title if contact else None),
                    email=email,
                    linkedin_url=person.get("linkedin_url") or linkedin_url,
                    verified_status=ver_status,
                    confidence=0.89 if ver_status == VerificationStatus.VALID else 0.72,
                    source="apollo",
                )
            )

        if not company_patch and not contacts:
            return None

        outcome = "person_email" if contacts else "company"
        credits_used, reported_credits = _resolve_credits_used(
            self.config,
            outcome=outcome,
            ver_status=ver_status,
            has_email=bool(email),
            payloads=(data, person, org),
        )

        return EnrichmentResult(
            source="apollo",
            stage="stage_2",
            confidence=0.86 if contacts else 0.78,
            verified_status=ver_status,
            credits_used=credits_used,
            company_patch=company_patch,
            contacts=contacts,
            raw_payload=_attach_credit_metadata(
                data or {},
                outcome=outcome,
                credits_used=credits_used,
                provider_reported_credits=reported_credits,
            ),
        )

    async def call_provider(
        self,
        provider: str,
        api_key: str,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> EnrichmentResult | None:
        """Signature matching Stage2ProviderCaller in enrichment/waterfall.py."""
        normalized_provider = (provider or "").strip().lower()
        target_contact = _pick_target_contact(lead, policy)
        headers = {"User-Agent": self.config.user_agent}
        timeout = httpx.Timeout(self.config.timeout_seconds)

        handlers = {
            "icypeas": self._enrich_icypeas,
            "findymail": self._enrich_findymail,
            "prospeo": self._enrich_prospeo,
            "apollo": self._enrich_apollo,
        }
        handler = handlers.get(normalized_provider)
        if handler is None:
            return None

        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            return await handler(client, api_key, lead, target_contact)

    async def __call__(
        self,
        provider: str,
        api_key: str,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> EnrichmentResult | None:
        return await self.call_provider(provider, api_key, lead, policy)


__all__ = ["ProviderHttpError", "Stage2ApiAdapter", "Stage2ApiConfig"]