"""
ADL-SignalForge Normalization Module (V5 Non-Destructive)
Path: agencies/adl/extraction/normalizers.py
"""

import re
import unicodedata
from typing import Any, Dict, Generator, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse, urljoin

# Pre-compiled regex patterns for performance optimization
WHITESPACE_REGEX = re.compile(r'\s+')
ALPHANUMERIC_FP_REGEX = re.compile(r'[^a-z0-9]+')
HTTP_SCHEME_REGEX = re.compile(r"^https?://", re.IGNORECASE)


class AgencyNormalizer:
    """
    Non-destructive V5 normalizer. Standardizes candidate fields, validates 
    URLs and provenance, and preserves individual observations without 
    premature entity merging.
    """
    
    EXCLUDED_DOMAINS: Set[str] = {
        "google.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "instagram.com",
        "youtube.com",
        "yelp.com",
        "wikipedia.org",
        "clutch.co",
        "g2.com",
        "capterra.com",
    }

    def __init__(self, raw_agencies: Iterable[Dict[str, Any]]) -> None:
        self.raw_agencies = raw_agencies

    @staticmethod
    def clean_name(name: Any) -> str:
        """Safely cleans and standardizes raw agency name text."""
        if not name or not isinstance(name, str):
            return ""
        normalized_unicode = unicodedata.normalize("NFKC", name)
        return WHITESPACE_REGEX.sub(' ', normalized_unicode).strip()

    @staticmethod
    def _parse_http_url(url: Optional[str]) -> Optional[Tuple[Any, str]]:
        """
        Robust URL parser and validator. Rejects credentials, invalid ports,
        malformed authorities, and non-HTTP(S) schemes.
        """
        if not isinstance(url, str):
            return None
        cleaned_url = url.strip()
        if not cleaned_url:
            return None

        if not HTTP_SCHEME_REGEX.match(cleaned_url):
            cleaned_url = "https://" + cleaned_url

        try:
            parsed = urlparse(cleaned_url)
            scheme = (parsed.scheme or "").lower()
            if scheme not in {"http", "https"}:
                return None

            hostname = parsed.hostname
            if not hostname:
                return None

            # Safer credential check: catch any '@' in netloc before hostname
            netloc = parsed.netloc or ""
            if "@" in netloc.split("@")[:-1]:  # Check userinfo section
                return None
            if parsed.username or parsed.password:
                return None

            # Validate port against scheme defaults
            try:
                port = parsed.port
            except ValueError:
                return None

            default_port = {"http": 80, "https": 443}.get(scheme)
            if port is not None and port == default_port:
                port = None  # Normalize away redundant default ports

            try:
                canonical_host = hostname.rstrip(".").lower().encode("idna").decode("ascii")
            except Exception:
                canonical_host = hostname.rstrip(".").lower()

            if not canonical_host:
                return None

            return parsed, canonical_host, port
        except Exception:
            return None

    @classmethod
    def canonicalize_domain(cls, domain: Optional[str]) -> Optional[str]:
        """Safely normalizes an external domain string."""
        if not domain or not isinstance(domain, str):
            return None
        cleaned = domain.strip().lower().rstrip(".")
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        try:
            return cleaned.encode("idna").decode("ascii")
        except Exception:
            return cleaned

    @classmethod
    def is_blocked_domain(cls, domain: Optional[str]) -> bool:
        """Performs exact or subdomain-level matching against excluded domains."""
        canonical = cls.canonicalize_domain(domain)
        if not canonical:
            return True
        return any(
            canonical == blocked or canonical.endswith("." + blocked)
            for blocked in cls.EXCLUDED_DOMAINS
        )

    @classmethod
    def normalize_url_detailed(cls, url: Optional[str]) -> Dict[str, Any]:
        """Canonicalizes a URL and returns structured website status metadata."""
        if not url:
            return {
                "website": None,
                "domain": None,
                "website_status": "missing",
                "website_rejection_reason": "no_url_provided"
            }

        parse_res = cls._parse_http_url(url)
        if not parse_res:
            return {
                "website": None,
                "domain": None,
                "website_status": "rejected",
                "website_rejection_reason": "malformed_url"
            }

        parsed, hostname, port = parse_res

        if cls.is_blocked_domain(hostname):
            return {
                "website": None,
                "domain": None,
                "website_status": "rejected",
                "website_rejection_reason": "excluded_platform"
            }

        scheme = parsed.scheme.lower()
        canonical_netloc = hostname
        if port is not None:
            canonical_netloc = f"{hostname}:{port}"

        normalized = urlunparse((scheme, canonical_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

        return {
            "website": normalized,
            "domain": hostname,
            "website_status": "accepted",
            "website_rejection_reason": None
        }

    @staticmethod
    def normalize_evidence(value: Any) -> List[Dict[str, Any]]:
        """Ensures evidence is structured correctly as a list of dictionaries."""
        if isinstance(value, dict):
            return [dict(value)]
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @classmethod
    def normalize_record(cls, record: Dict[str, Any]) -> Dict[str, Any]:
        """Non-mutating record normalization preserving nested data structures and provenance."""
        if not isinstance(record, dict):
            return {}

        normalized = dict(record)

        # Standardize name
        raw_name = normalized.get("agency_name", "")
        normalized["agency_name"] = cls.clean_name(raw_name)

        # Standardize website
        raw_website = normalized.get("website")
        url_metadata = cls.normalize_url_detailed(raw_website)
        normalized.update(url_metadata)

        # Standardize description
        description = normalized.get("description", "")
        if isinstance(description, str):
            normalized["description"] = WHITESPACE_REGEX.sub(' ', description).strip()
        else:
            normalized["description"] = ""

        # Independent confidence tracking
        confidences = normalized.get("confidences")
        if not isinstance(confidences, dict):
            confidences = {
                "name_confidence": normalized.get("name_confidence", normalized.get("confidence", "medium")),
                "website_confidence": normalized.get("website_confidence", "medium"),
                "description_confidence": normalized.get("description_confidence", "medium")
            }
        
        if not normalized.get("website"):
            confidences["website_confidence"] = "low"
        
        normalized["confidences"] = confidences

        # Provenance source URLs
        source_url = normalized.get("source_url")
        source_urls = normalized.get("source_urls")
        if isinstance(source_urls, list):
            normalized["source_urls"] = [str(u) for u in source_urls if u]
        elif source_url:
            normalized["source_urls"] = [str(source_url)]
        else:
            normalized["source_urls"] = []

        # Structured evidence
        normalized["extraction_evidence"] = cls.normalize_evidence(normalized.get("extraction_evidence"))

        # Generate non-assertive deterministic matching fingerprint for downstream entity resolution
        name_fp = ALPHANUMERIC_FP_REGEX.sub("", normalized["agency_name"].casefold())
        normalized["matching_fingerprint"] = {
            "domain": normalized.get("domain"),
            "name_fingerprint": name_fp if name_fp else None
        }

        return normalized

    def process(self) -> Dict[str, Any]:
        """
        Processes raw candidates iteratively without performing destructive entity merging.
        Returns standardized records and rich outcome telemetry stats.
        """
        input_count = 0
        records: List[Dict[str, Any]] = []
        
        accepted_count = 0
        rejected_count = 0
        missing_website_count = 0
        rejected_website_count = 0
        invalid_type_count = 0
        missing_name_count = 0
        rejection_reasons: Dict[str, int] = {}

        for item in self.raw_agencies:
            input_count += 1
            if not isinstance(item, dict):
                invalid_type_count += 1
                rejected_count += 1
                continue

            candidate = self.normalize_record(item)
            if not candidate:
                rejected_count += 1
                continue

            name = candidate.get("agency_name")
            if not name:
                missing_name_count += 1
                rejected_count += 1
                continue

            status = candidate.get("website_status")
            if status == "missing":
                missing_website_count += 1
            elif status == "rejected":
                rejected_website_count += 1
                reason = candidate.get("website_rejection_reason", "unknown")
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

            records.append(candidate)
            accepted_count += 1

        stats = {
            "input_count": input_count,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "missing_website_count": missing_website_count,
            "rejected_website_count": rejected_website_count,
            "rejections": {
                "invalid_record_type": invalid_type_count,
                "missing_agency_name": missing_name_count,
                "website_reasons": rejection_reasons
            }
        }

        return {
            "records": records,
            "stats": stats
        }


# ==========================================
# Module-level wrappers
# ==========================================

def normalize_domain(url: Optional[str]) -> Optional[str]:
    res = AgencyNormalizer.normalize_url_detailed(url)
    return res.get("domain")

def is_blocked_domain(domain: Optional[str]) -> bool:
    return AgencyNormalizer.is_blocked_domain(domain)

def is_valid_agency_url(url: Optional[str], base_url: Optional[str] = None) -> bool:
    full_url = urljoin(base_url, url) if base_url and url else url
    res = AgencyNormalizer.normalize_url_detailed(full_url)
    return res.get("website_status") == "accepted"