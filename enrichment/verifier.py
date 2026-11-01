import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
import dns.asyncresolver  # Requires dnspython package

from .schemas import (
    AgencyEnrichmentProfile,
    EmailCandidate,
    DomainMailStatus,
    VerificationReport,
)

# Conservative RFC-aligned corporate email syntax validation pattern
EMAIL_SYNTAX_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)


def normalize_name_part(part: str) -> str:
  """Normalizes international names using Unicode NFKD and safe ASCII transliteration."""
  if not part:
    return ""
  normalized = unicodedata.normalize("NFKD", part)
  ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
  return re.sub(r"[^a-zA-Z]", "", ascii_name).lower()


def sanitize_canonical_domain(domain_input: str) -> str | None:
  """Thoroughly parses and sanitizes domain inputs using urlsplit to reject malformed hostnames."""
  if not domain_input:
    return None
  
  raw = domain_input.strip()
  if not raw.startswith(("http://", "https://")):
    raw = f"https://{raw}"

  try:
    parsed = urlsplit(raw)
    hostname = parsed.hostname
    if not hostname or "." not in hostname:
      return None
    # Reject IP addresses or anomalous labels
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", hostname):
      return None
    return hostname.lower()
  except Exception:
    return None


def validate_syntax(email: str) -> bool:
  """Performs rigorous syntax checks adhering to practical corporate email limits."""
  if not email or len(email) > 254:
    return False
  
  parts = email.split("@")
  if len(parts) != 2:
    return False
  
  local_part, domain_part = parts[0], parts[1]
  if len(local_part) > 64 or len(local_part) == 0:
    return False
  if ".." in local_part or ".." in domain_part:
    return False
    
  return bool(EMAIL_SYNTAX_REGEX.match(email))


async def resolve_domain_mail_infrastructure(
    domain: str, resolver: dns.asyncresolver.Resolver
) -> tuple[DomainMailStatus, list[str]]:
  """Inspects DNS MX records, null MX configurations, and A/AAAA implicit fallbacks."""
  clean_domain = sanitize_canonical_domain(domain)
  if not clean_domain:
    return "invalid_domain", []

  mx_hosts: list[str] = []

  # 1. Check explicit MX records
  try:
    answers = await resolver.resolve(clean_domain, "MX")
    for rdata in answers:
      exchange = str(rdata.exchange).rstrip(".")
      preference = rdata.preference
      # Check for Null MX RFC 7505 (preference 0 and exchange is a single dot '.')
      if preference == 0 and exchange == ".":
        return "null_mx", []
      mx_hosts.append(exchange)
      
    if mx_hosts:
      return "mx_present", mx_hosts
  except dns.resolver.NoAnswer:
    # No explicit MX records; check for implicit MX (A/AAAA fallback) per SMTP standard
    try:
      a_answers = await resolver.resolve(clean_domain, "A")
      if a_answers:
        return "implicit_mx", [clean_domain]
    except Exception:
      pass
    return "nxdomain", []
  except dns.resolver.NXDOMAIN:
    return "nxdomain", []
  except dns.resolver.Timeout:
    return "dns_timeout", []
  except Exception:
    return "dns_error", []

  return "nxdomain", []


def generate_permutations(first_name: str, last_name: str, domain: str) -> list[tuple[str, str]]:
  """Generates unique, deduplicated standard corporate email permutations."""
  f = normalize_name_part(first_name)
  l = normalize_name_part(last_name)
  d = sanitize_canonical_domain(domain)

  if not f or not l or not d:
    return []

  raw_perms = [
      ("first.last", f"{f}.{l}@{d}"),
      ("first", f"{f}@{d}"),
      ("flast", f"{f[0]}{l}@{d}"),
      ("firstl", f"{f}{l[0]}@{d}"),
      ("last.first", f"{l}.{f}@{d}"),
      ("last", f"{l}@{d}"),
  ]

  # Deduplicate while preserving sequence order
  seen = set()
  unique_perms = []
  for perm_type, email in raw_perms:
    if email not in seen:
      seen.add(email)
      unique_perms.append((perm_type, email))

  return unique_perms


async def verify_agency_emails(
    profile: AgencyEnrichmentProfile
) -> VerificationReport:
  """Orchestrates candidate generation, DNS mail-routing inspection, and evidence-backed reporting."""
  domain = profile.canonical_domain
  clean_domain = sanitize_canonical_domain(domain)

  if not clean_domain:
    return VerificationReport(
        company_id=profile.company_id,
        canonical_domain=domain,
        email_candidates=[],
        verification_metadata={"error": "Invalid or unresolvable canonical domain provided."},
    )

  # Configure reusable async DNS resolver with safe bounded timeouts
  resolver = dns.asyncresolver.Resolver()
  resolver.timeout = 3.0
  resolver.lifetime = 3.0

  mail_status, mx_hosts = await resolve_domain_mail_infrastructure(clean_domain, resolver)

  # Collect already observed emails from extractor to avoid regenerating known addresses
  observed_emails = {contact.value.lower() for contact in profile.contacts}
  candidates: list[EmailCandidate] = []
  seen_candidate_emails = set()

  # 1. First, record explicitly observed extractor contacts as first-party evidence
  for contact in profile.contacts:
    if contact.value.lower() in seen_candidate_emails:
      continue
    seen_candidate_emails.add(contact.value.lower())
    
    syntax_ok = validate_syntax(contact.value)
    candidates.append(
        EmailCandidate(
            full_name="Observed Entity / General Contact",
            email=contact.value.lower(),
            permutation_type="observed_source",
            syntax_valid=syntax_ok,
            domain_mail_status=mail_status,
            evidence_level="observed",
            source_reference=contact.source_url,
        )
    )

  # 2. Second, generate candidate permutations for identified decision-makers
  for dm in profile.decision_makers:
    if not dm.full_name:
      continue

    name_parts = dm.full_name.strip().split()
    if len(name_parts) < 2:
      continue  # Skip mononyms or unparseable name tokens

    first_name = name_parts[0]
    last_name = name_parts[-1]

    perms = generate_permutations(first_name, last_name, clean_domain)

    for perm_type, email_addr in perms:
      if email_addr in observed_emails or email_addr in seen_candidate_emails:
        continue  # Skip if already captured as an observed contact

      seen_candidate_emails.add(email_addr)
      syntax_ok = validate_syntax(email_addr)

      # Determine strict evidence classification based on DNS outcomes
      if not syntax_ok:
        evidence_lvl = "unverified"
      elif mail_status in ("mx_present", "implicit_mx"):
        evidence_lvl = "domain_valid_generated"
      else:
        evidence_lvl = "unverified"

      candidates.append(
          EmailCandidate(
              full_name=dm.full_name,
              email=email_addr,
              permutation_type=perm_type,
              syntax_valid=syntax_ok,
              domain_mail_status=mail_status,
              evidence_level=evidence_lvl,
              source_reference=f"Extracted from {dm.source_url or 'company profile'} via permutation model",
          )
      )

  return VerificationReport(
      company_id=profile.company_id,
      canonical_domain=clean_domain,
      email_candidates=candidates,
      domain_mx_hosts=mx_hosts,
      verification_metadata={
          "domain_mail_status": mail_status,
          "total_candidates_generated": len(candidates),
          "observed_contacts_retained": len(observed_emails),
          "verified_at": datetime.now(timezone.utc).isoformat(),
      },
  )