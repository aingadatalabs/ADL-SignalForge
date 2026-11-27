"""Stage 1 zero-cost DNS (MX/TXT/SPF/DMARC) and HTTP/tech-stack signal extractor."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..base import (
    EnrichmentPolicy,
    EnrichmentResult,
    LeadRecord,
    VerificationStatus,
    normalize_domain,
)

try:
    import dns.asyncresolver  # type: ignore[import-untyped]

    _HAS_DNSPYTHON = True
except ImportError:
    _HAS_DNSPYTHON = False


MX_PROVIDER_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("aspmx.l.google.com", "Google Workspace"),
    ("googlemail.com", "Google Workspace"),
    ("protection.outlook.com", "Microsoft 365"),
    ("mail.protection.outlook.com", "Microsoft 365"),
    ("zoho.com", "Zoho Mail"),
    ("zoho.eu", "Zoho Mail"),
    ("protonmail.ch", "Proton Mail"),
    ("mimecast.com", "Mimecast (Enterprise Email Security)"),
    ("pphosted.com", "Proofpoint (Enterprise Email Security)"),
    ("messagelabs.com", "Broadcom/Symantec Email Security"),
    ("secureserver.net", "GoDaddy Email"),
    ("emailsrvr.com", "Rackspace Email"),
    ("fastmail.com", "Fastmail"),
    ("improvmx.com", "ImprovMX"),
)

# Tuple of (marker, tech_label, case_sensitive)
TXT_STACK_SIGNATURES: tuple[tuple[str, str, bool], ...] = (
    ("hubspot", "HubSpot", False),
    ("salesforce", "Salesforce", False),
    ("pardot", "Salesforce Pardot", False),
    ("marketo", "Adobe Marketo", False),
    ("klaviyo", "Klaviyo", False),
    ("mailchimp", "Mailchimp", False),
    ("mandrillapp", "Mailchimp Transactional", False),
    ("sendgrid", "SendGrid", False),
    ("postmarkapp", "Postmark", False),
    ("amazonses", "Amazon SES", False),
    ("mailgun", "Mailgun", False),
    ("brevo", "Brevo", False),
    ("sendinblue", "Brevo", False),
    ("activecampaign", "ActiveCampaign", False),
    ("intercom", "Intercom", False),
    ("zendesk", "Zendesk", False),
    ("freshdesk", "Freshdesk", False),
    ("helpscout", "Help Scout", False),
    ("stripe-verification", "Stripe", False),
    ("atlassian-domain-verification", "Atlassian", False),
    ("notion-domain-verification", "Notion", False),
    ("slack-domain-verification", "Slack", False),
    ("miro-verification", "Miro", False),
    ("figma-domain-verification", "Figma", False),
    ("docusign", "DocuSign", False),
    ("MS=", "Microsoft 365", True),
    ("google-site-verification", "Google Search Console / Workspace", False),
    ("facebook-domain-verification", "Meta Business", False),
    ("apple-domain-verification", "Apple Business", False),
)

HEADER_TECH_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("cf-ray", "", "Cloudflare"),
    ("x-vercel-id", "", "Vercel"),
    ("x-nf-request-id", "", "Netlify"),
    ("x-amz-cf-id", "", "AWS CloudFront"),
    ("x-shopify-stage", "", "Shopify"),
    ("x-wix-request-id", "", "Wix"),
    ("x-webflow-request-id", "", "Webflow"),
    ("x-github-request-id", "", "GitHub Pages"),
    ("server", "cloudflare", "Cloudflare"),
    ("server", "nginx", "Nginx"),
    ("server", "apache", "Apache"),
    ("server", "openresty", "OpenResty"),
    ("x-powered-by", "express", "Express.js"),
    ("x-powered-by", "next.js", "Next.js"),
    ("x-powered-by", "php", "PHP"),
    ("x-powered-by", "asp.net", "ASP.NET"),
    ("x-generator", "wordpress", "WordPress"),
    ("x-generator", "drupal", "Drupal"),
)

HTML_MARKER_SIGNATURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"wp-content/|wp-includes/", re.I), "WordPress"),
    (re.compile(r"cdn\.shopify\.com", re.I), "Shopify"),
    (re.compile(r"assets\.website-files\.com|data-wf-site", re.I), "Webflow"),
    (re.compile(r"/_next/static/", re.I), "Next.js"),
    (re.compile(r"js\.hs-scripts\.com|hs-analytics", re.I), "HubSpot"),
    (re.compile(r"googletagmanager\.com/gtm\.js", re.I), "Google Tag Manager"),
    (re.compile(r"gtag/js\?id=G-", re.I), "Google Analytics 4"),
    (re.compile(r"connect\.facebook\.net/.*/fbevents\.js", re.I), "Meta Pixel"),
    (re.compile(r"snap\.licdn\.com/li\.lms-analytics", re.I), "LinkedIn Insight Tag"),
    (re.compile(r"widget\.intercom\.io", re.I), "Intercom"),
    (re.compile(r"static\.zdassets\.com", re.I), "Zendesk"),
    (re.compile(r"js\.stripe\.com", re.I), "Stripe"),
    (re.compile(r"cdn\.segment\.com", re.I), "Segment"),
    (re.compile(r"plausible\.io/js/", re.I), "Plausible Analytics"),
)

QUOTED_SEGMENT_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
)


@dataclass
class DnsTechConfig:
    dns_timeout_seconds: float = 4.0
    http_timeout_seconds: float = 8.0
    max_html_bytes: int = 350_000
    max_redirects: int = 5
    allow_private_targets: bool = False
    user_agent: str = "ADL-SignalForge-TechProbe/1.0 (+https://aingadatalabs.com)"


def _host_matches_domain_suffix(host: str, suffix: str) -> bool:
    """Return True if `host` equals `suffix` or is a subdomain `*.<suffix>`."""
    h = host.lower().rstrip(".")
    s = suffix.lower().rstrip(".")
    if not h or not s:
        return False
    return h == s or h.endswith(f".{s}")


def _txt_marker_matches(marker: str, record_text: str, *, case_sensitive: bool) -> bool:
    """
    Match a TXT signature marker on clean token/domain/assignment boundaries rather than
    arbitrary mid-word substrings.
    """
    if not marker or not record_text:
        return False

    haystack = record_text if case_sensitive else record_text.lower()
    needle = marker if case_sensitive else marker.lower()

    if needle.endswith("="):
        pattern = re.compile(rf"(?:^|[\s;\"]){re.escape(needle)}")
        return bool(pattern.search(haystack))

    if "-verification" in needle:
        pattern = re.compile(rf"(?:^|[\s;\"=]){re.escape(needle)}(?:$|[\s;\"=])")
        return bool(pattern.search(haystack))

    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")
    return bool(pattern.search(haystack))


def _header_token_matches(expected_substr: str, header_value: str) -> bool:
    """
    Match an expected header signature token on token/version/delimiter boundaries
    so substrings inside unrelated words do not trigger false positives.
    """
    if not expected_substr:
        return True
    cleaned_val = header_value.strip().lower()
    cleaned_tok = expected_substr.strip().lower()
    if not cleaned_val or not cleaned_tok:
        return False

    pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(cleaned_tok)}(?=$|[\s/;,(]|$)")
    return bool(pattern.search(cleaned_val))


def _is_disallowed_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP address is loopback, private RFC1918/ULA, link-local, or non-routable."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )


async def _is_safe_probe_target(url: str, *, allow_private_targets: bool = False) -> bool:
    """Validate URL scheme, hostname, and resolved IP addresses against SSRF targets."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host or host == "localhost":
        return allow_private_targets
    if any(host.endswith(suf) for suf in BLOCKED_HOST_SUFFIXES):
        return allow_private_targets

    try:
        literal_ip = ipaddress.ip_address(host)
        if _is_disallowed_ip(literal_ip) and not allow_private_targets:
            return False
        return True
    except ValueError:
        pass

    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False

    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for entry in addr_info:
        sockaddr = entry[4]
        if sockaddr and sockaddr[0]:
            try:
                resolved_ips.append(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
            except ValueError:
                continue

    if not resolved_ips:
        return False

    if not allow_private_targets and any(_is_disallowed_ip(ip) for ip in resolved_ips):
        return False

    return True


def _join_dns_txt_chunks(raw: str) -> str:
    """
    Reconstruct a contiguous TXT record string from DNS/DoH representation where
    character-strings >255 bytes may be split across multiple quoted chunks.
    """
    trimmed = raw.strip()
    if not trimmed:
        return ""
    if '"' in trimmed:
        quoted_parts = QUOTED_SEGMENT_RE.findall(trimmed)
        if quoted_parts:
            unquoted = "".join(
                re.sub(r'\\(.)', r"\1", seg) for seg in quoted_parts
            )
            remainder = QUOTED_SEGMENT_RE.sub("", trimmed).strip()
            return f"{unquoted}{remainder}" if remainder else unquoted
    return re.sub(r"\s+", " ", trimmed).strip()


def classify_mx_records(mx_hosts: list[str]) -> tuple[str | None, list[str]]:
    """
    Infer primary email provider and security gateways from MX hosts (ordered by
    preference ascending when collected by `_query_dns_records`).
    """
    detected: list[str] = []
    for host in mx_hosts:
        h = host.lower().rstrip(".")
        for suffix, label in MX_PROVIDER_SIGNATURES:
            if _host_matches_domain_suffix(h, suffix) and label not in detected:
                detected.append(label)
    primary = detected[0] if detected else ("Custom / Self-Hosted MX" if mx_hosts else None)
    return primary, detected


def extract_stack_from_txt(txt_records: list[str]) -> tuple[list[str], bool, list[str]]:
    """Extract third-party SaaS tools, SPF presence, and SPF includes from TXT records."""
    tools: list[str] = []
    spf_includes: list[str] = []
    has_spf = False

    for raw in txt_records:
        joined = _join_dns_txt_chunks(raw)
        if not joined:
            continue
        lower = joined.lower()

        if lower.startswith("v=spf1"):
            has_spf = True
            for inc in re.findall(r"include:([^\s]+)", lower):
                if inc not in spf_includes:
                    spf_includes.append(inc)

        for marker, tool_name, case_sensitive in TXT_STACK_SIGNATURES:
            if _txt_marker_matches(marker, joined, case_sensitive=case_sensitive) and tool_name not in tools:
                tools.append(tool_name)

    return tools, has_spf, spf_includes


def detect_tech_from_http(
    headers: dict[str, str],
    html_snippet: str = "",
) -> tuple[list[str], dict[str, bool]]:
    """Detect web stack & security posture from HTTP response headers and HTML markers."""
    lowered_headers = {k.lower(): v for k, v in headers.items()}
    detected: list[str] = []

    for header_name, expected_substr, tech_label in HEADER_TECH_SIGNATURES:
        val = lowered_headers.get(header_name)
        if val is None:
            continue
        if _header_token_matches(expected_substr, val) and tech_label not in detected:
            detected.append(tech_label)

    for pattern, tech_label in HTML_MARKER_SIGNATURES:
        if pattern.search(html_snippet) and tech_label not in detected:
            detected.append(tech_label)

    security_posture = {
        "has_hsts": "strict-transport-security" in lowered_headers,
        "has_csp": "content-security-policy" in lowered_headers,
        "has_x_frame_options": "x-frame-options" in lowered_headers,
    }
    return detected, security_posture


def compute_digital_maturity_score(
    *,
    mx_hosts: list[str],
    has_spf: bool,
    has_dmarc: bool,
    tech_stack: list[str],
    security_posture: dict[str, bool],
    https_reachable: bool,
) -> tuple[float, list[str]]:
    """Score operational/technical maturity (0.0 - 1.0) and list observable gaps."""
    score = 0.0
    gaps: list[str] = []

    if https_reachable:
        score += 0.25
    else:
        gaps.append("https_unreachable")

    if mx_hosts:
        score += 0.20
    else:
        gaps.append("no_mx_records")

    if has_spf:
        score += 0.10
    else:
        gaps.append("missing_spf")

    if has_dmarc:
        score += 0.15
    else:
        gaps.append("missing_dmarc")

    if security_posture.get("has_hsts"):
        score += 0.05
    if security_posture.get("has_csp"):
        score += 0.05
    else:
        gaps.append("missing_csp_header")

    stack_bonus = min(0.20, len(tech_stack) * 0.03)
    score += stack_bonus
    if len(tech_stack) <= 1:
        gaps.append("minimal_marketing_or_analytics_stack")

    return round(min(1.0, score), 3), gaps


class DnsTechProvider:
    """Stage 1 fetcher for DNS (MX/TXT/DMARC) and HTTP/HTML technographic signals."""

    def __init__(self, config: DnsTechConfig | None = None) -> None:
        self.config = config or DnsTechConfig()

    async def _query_dns_records(
        self,
        name: str,
        rdtype: str,
        *,
        doh_client: httpx.AsyncClient | None = None,
    ) -> list[str]:
        rd_upper = rdtype.upper()
        if _HAS_DNSPYTHON:
            resolver = dns.asyncresolver.Resolver()
            resolver.lifetime = self.config.dns_timeout_seconds
            try:
                answers = await resolver.resolve(name, rd_upper)
                if rd_upper == "MX":
                    mx_entries: list[tuple[int, str]] = []
                    for rdata in answers:
                        pref = int(getattr(rdata, "preference", 10))
                        exchange = str(rdata.exchange).rstrip(".")
                        if exchange:
                            mx_entries.append((pref, exchange))
                    mx_entries.sort(key=lambda item: item[0])
                    return [exchange for _, exchange in mx_entries]

                records: list[str] = []
                for rdata in answers:
                    if rd_upper == "TXT" and hasattr(rdata, "strings"):
                        chunks = [
                            b.decode("utf-8", errors="replace")
                            for b in (rdata.strings or [])
                            if isinstance(b, (bytes, bytearray))
                        ]
                        joined_txt = "".join(chunks).strip()
                        if joined_txt:
                            records.append(joined_txt)
                    else:
                        records.append(
                            _join_dns_txt_chunks(str(rdata))
                            if rd_upper == "TXT"
                            else str(rdata).strip('"')
                        )
                return records
            except Exception:
                return []

        async def _run_doh(active_client: httpx.AsyncClient) -> list[str]:
            try:
                resp = await active_client.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": name, "type": rd_upper},
                    headers={"Accept": "application/dns-json"},
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                answers = data.get("Answer") or []
                if rd_upper == "MX":
                    mx_entries = []
                    for ans in answers:
                        raw = str(ans.get("data") or "").strip()
                        if not raw:
                            continue
                        parts = raw.split()
                        if len(parts) >= 2:
                            try:
                                pref = int(parts[0])
                            except ValueError:
                                pref = 10
                            exchange = parts[-1].rstrip(".")
                        else:
                            pref = 10
                            exchange = parts[0].rstrip(".")
                        if exchange:
                            mx_entries.append((pref, exchange))
                    mx_entries.sort(key=lambda item: item[0])
                    return [exchange for _, exchange in mx_entries]

                out: list[str] = []
                for ans in answers:
                    raw = str(ans.get("data") or "").strip()
                    if not raw:
                        continue
                    if rd_upper == "TXT":
                        joined_txt = _join_dns_txt_chunks(raw)
                        if joined_txt:
                            out.append(joined_txt)
                    else:
                        out.append(raw.strip('"'))
                return out
            except Exception:
                return []

        if doh_client is not None:
            return await _run_doh(doh_client)

        async with httpx.AsyncClient(timeout=self.config.dns_timeout_seconds) as owned_client:
            return await _run_doh(owned_client)

    async def _probe_http(
        self, domain: str
    ) -> tuple[bool, bool, str | None, int | None, dict[str, str], str]:
        """
        Probe https and http for `domain` with SSRF target & redirect validation.
        Returns `(https_healthy, any_2xx_healthy, resolved_url, status_code, headers, html_snippet)`.
        """
        headers = {"User-Agent": self.config.user_agent, "Accept": "text/html,application/xhtml+xml"}
        timeout = httpx.Timeout(self.config.http_timeout_seconds)
        fallback_observation: tuple[bool, bool, str | None, int | None, dict[str, str], str] = (
            False,
            False,
            None,
            None,
            {},
            "",
        )

        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as client:
            for scheme in ("https", "http"):
                current_url = f"{scheme}://{domain}"
                if not await _is_safe_probe_target(
                    current_url, allow_private_targets=self.config.allow_private_targets
                ):
                    continue

                try:
                    hops = 0
                    resp: httpx.Response | None = None
                    while True:
                        candidate_resp = await client.get(current_url)
                        if candidate_resp.status_code in {301, 302, 303, 307, 308}:
                            location = candidate_resp.headers.get("location")
                            if not location or hops >= self.config.max_redirects:
                                resp = candidate_resp
                                break
                            next_url = urljoin(current_url, location.strip())
                            if not await _is_safe_probe_target(
                                next_url, allow_private_targets=self.config.allow_private_targets
                            ):
                                resp = None
                                break
                            current_url = next_url
                            hops += 1
                            continue
                        resp = candidate_resp
                        break

                    if resp is None:
                        continue

                    status = resp.status_code
                    final_url = current_url
                    resp_headers = dict(resp.headers)
                    body = resp.text[: self.config.max_html_bytes] if resp.text else ""
                    is_2xx = 200 <= status < 300
                    is_https_healthy = is_2xx and final_url.lower().startswith("https://")

                    if is_2xx:
                        return (is_https_healthy, True, final_url, status, resp_headers, body)

                    if fallback_observation[3] is None:
                        fallback_observation = (False, False, final_url, status, resp_headers, body)
                except httpx.HTTPError:
                    continue

        return fallback_observation

    async def fetch_lead_signals(
        self,
        lead: LeadRecord,
        policy: EnrichmentPolicy,
    ) -> list[EnrichmentResult]:
        domain = normalize_domain(lead.domain)
        if not domain:
            return []

        if _HAS_DNSPYTHON:
            mx_hosts, txt_records, dmarc_records, (https_ok, site_healthy, final_url, http_status, resp_headers, html_snippet) = (
                await asyncio.gather(
                    self._query_dns_records(domain, "MX"),
                    self._query_dns_records(domain, "TXT"),
                    self._query_dns_records(f"_dmarc.{domain}", "TXT"),
                    self._probe_http(domain),
                )
            )
        else:
            async with httpx.AsyncClient(timeout=self.config.dns_timeout_seconds) as shared_doh_client:
                (
                    mx_hosts,
                    txt_records,
                    dmarc_records,
                    (https_ok, site_healthy, final_url, http_status, resp_headers, html_snippet),
                ) = await asyncio.gather(
                    self._query_dns_records(domain, "MX", doh_client=shared_doh_client),
                    self._query_dns_records(domain, "TXT", doh_client=shared_doh_client),
                    self._query_dns_records(f"_dmarc.{domain}", "TXT", doh_client=shared_doh_client),
                    self._probe_http(domain),
                )

        if not mx_hosts and not txt_records and http_status is None:
            return []

        primary_email_provider, all_mx_providers = classify_mx_records(mx_hosts)
        txt_tools, has_spf, spf_includes = extract_stack_from_txt(txt_records)
        has_dmarc = any("v=dmarc1" in _join_dns_txt_chunks(r).lower() for r in dmarc_records)
        web_tech, security_posture = detect_tech_from_http(resp_headers, html_snippet)

        combined_stack: list[str] = []
        for item in [*all_mx_providers, *txt_tools, *web_tech]:
            if item not in combined_stack:
                combined_stack.append(item)

        maturity_score, technical_gaps = compute_digital_maturity_score(
            mx_hosts=mx_hosts,
            has_spf=has_spf,
            has_dmarc=has_dmarc,
            tech_stack=combined_stack,
            security_posture=security_posture,
            https_reachable=https_ok,
        )

        technographics = {
            "email_provider": primary_email_provider,
            "mx_hosts": mx_hosts,
            "has_spf": has_spf,
            "has_dmarc": has_dmarc,
            "spf_includes": spf_includes,
            "tech_stack": combined_stack,
            "security_posture": security_posture,
            "digital_maturity_score": maturity_score,
            "technical_gaps": technical_gaps,
            "http_status": http_status,
            "http_healthy": site_healthy,
            "resolved_url": final_url if site_healthy else None,
            "observed_url": final_url,
        }

        company_patch: dict[str, Any] = {
            "domain": domain,
            "extra": {"technographics": technographics},
        }
        if site_healthy and final_url and not lead.website:
            company_patch["website"] = final_url

        return [
            EnrichmentResult(
                source="dns_tech",
                stage="stage_1",
                confidence=0.85,
                verified_status=VerificationStatus.UNVERIFIED,
                credits_used=0.0,
                company_patch=company_patch,
                raw_payload=technographics,
            )
        ]

    async def __call__(self, lead: LeadRecord, policy: EnrichmentPolicy) -> list[EnrichmentResult]:
        return await self.fetch_lead_signals(lead, policy)


__all__ = [
    "DnsTechConfig",
    "DnsTechProvider",
    "classify_mx_records",
    "compute_digital_maturity_score",
    "detect_tech_from_http",
    "extract_stack_from_txt",
]