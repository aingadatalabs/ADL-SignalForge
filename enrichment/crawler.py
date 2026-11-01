import asyncio
from datetime import datetime, timezone
import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse, urljoin
import httpx

from .schemas import CrawlResult, CrawledPage, FetchStatus, NormalizedCandidate

# High-value static discovery target paths (Stage 1)
TARGET_PATHS = [
    "/",
    "/about",
    "/about-us",
    "/team",
    "/our-team",
    "/leadership",
    "/contact",
    "/contact-us",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def validate_and_normalize_domain(domain_input: str) -> str | None:
  """Validates and normalizes a canonical domain, preventing malformed inputs."""
  if not domain_input:
    return None

  cleaned = domain_input.strip().lower()
  if not cleaned.startswith(("http://", "https://")):
    cleaned = f"https://{cleaned}"

  parsed = urlparse(cleaned)
  hostname = parsed.hostname

  if not hostname or "." not in hostname:
    return None

  # Reject credentials in URLs
  if parsed.username or parsed.password:
    return None

  return f"{parsed.scheme}://{hostname}"


async def is_safe_destination(url: str) -> bool:
  """SSRF Protection: Rejects private IPs, loopback, and local networks."""
  try:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
      return False

    # Block obvious local indicators
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
      return False

    # Resolve IP address to check against private/reserved ranges
    loop = asyncio.get_running_loop()
    ip_addr_str = await loop.getaddrinfo(
        hostname, None, family=socket.AF_INET
    )
    if not ip_addr_str:
      return False

    ip_str = ip_addr_str[0][4][0]
    ip_obj = ipaddress.ip_address(ip_str)

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
      return False

    return True
  except Exception:
    return False


def classify_error(exc: Exception) -> FetchStatus:
  """Maps network/HTTP exceptions to structured fetch status codes."""
  if isinstance(exc, httpx.TimeoutException):
    return "timeout"
  elif isinstance(exc, httpx.TooManyRedirects):
    return "redirect_rejected"
  elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
    return "connection_error"
  return "connection_error"


async def fetch_page(
    client: httpx.AsyncClient, base_url: str, path: str
) -> CrawledPage:
  """Safely fetches a single subpage, ensuring SSRF validation and structured result capture."""
  url = urljoin(base_url, path)
  fetched_at = datetime.now(timezone.utc).isoformat()

  # Enforce SSRF protection before dispatching request
  if not await is_safe_destination(url):
    return CrawledPage(
        url=url,
        path=path,
        fetch_status="redirect_rejected",
        error="Target resolved to restricted or private network destination.",
        fetched_at=fetched_at,
    )

  try:
    response = await client.get(url)
    final_url = str(response.url)

    # Re-validate destination post-redirect
    if not await is_safe_destination(final_url):
      return CrawledPage(
          url=final_url,
          path=path,
          fetch_status="redirect_rejected",
          error="Redirected to restricted or private network destination.",
          fetched_at=fetched_at,
      )

    status_code = response.status_code
    content_type = response.headers.get("content-type", "")

    # Categorize status codes into structured fetch statuses
    if status_code == 200:
      if "text/html" not in content_type and "application/xhtml" not in content_type:
        return CrawledPage(
            url=final_url,
            path=path,
            fetch_status="invalid_content",
            status_code=status_code,
            content_type=content_type,
            error=f"Non-HTML content type received: {content_type}",
            fetched_at=fetched_at,
        )

      html_text = response.text
      content_hash = hashlib.sha256(html_text.encode("utf-8")).hexdigest()

      return CrawledPage(
          url=final_url,
          path=path,
          fetch_status="success",
          status_code=status_code,
          content_type=content_type,
          content_hash=content_hash,
          raw_html_ref=None,  # Will be populated by storage layer handler
          cleaned_text=None,  # Reserved for text cleaning stage
          fetched_at=fetched_at,
      )

    elif status_code == 404:
      status: FetchStatus = "not_found"
    elif status_code in (401, 403):
      status = "forbidden"
    elif status_code == 429:
      status = "rate_limited"
    elif status_code in (405, 406, 410):
      status = "blocked"
    else:
      status = "connection_error"

    return CrawledPage(
        url=final_url,
        path=path,
        fetch_status=status,
        status_code=status_code,
        content_type=content_type,
        error=f"HTTP Error Status: {status_code}",
        fetched_at=fetched_at,
    )

  except Exception as exc:
    return CrawledPage(
        url=url,
        path=path,
        fetch_status=classify_error(exc),
        error=str(exc),
        fetched_at=fetched_at,
    )


async def crawl_domain(
    candidate: NormalizedCandidate, client: httpx.AsyncClient
) -> CrawlResult:
  """Coordinates static subpage discovery and evidence collection for a normalized candidate."""
  started_at = datetime.now(timezone.utc).isoformat()
  
  base_url = validate_and_normalize_domain(candidate.canonical_domain)
  if not base_url:
    return CrawlResult(
        company_id=candidate.company_id,
        canonical_domain=candidate.canonical_domain,
        crawl_metadata={
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error": "Invalid or unresolvable canonical domain.",
        },
        pages=[],
    )

  # Concurrently fetch all static target paths for this candidate
  tasks = [fetch_page(client, base_url, path) for path in TARGET_PATHS]
  pages = await asyncio.gather(*tasks)

  completed_at = datetime.now(timezone.utc).isoformat()
  pages_fetched = sum(1 for p in pages if p.fetch_status == "success")
  pages_failed = len(pages) - pages_fetched

  return CrawlResult(
      company_id=candidate.company_id,
      canonical_domain=candidate.canonical_domain,
      crawl_metadata={
          "crawl_id": f"crawl_{candidate.company_id}_{int(datetime.now(timezone.utc).timestamp())}",
          "started_at": started_at,
          "completed_at": completed_at,
          "pages_attempted": len(TARGET_PATHS),
          "pages_fetched": pages_fetched,
          "pages_failed": pages_failed,
      },
      pages=list(pages),
  )
async def _test_run() -> None:
  # Quick standalone test runner for development
  client = httpx.AsyncClient(
      headers=DEFAULT_HEADERS,
      timeout=httpx.Timeout(10.0, connect=5.0),
      follow_redirects=True,
      verify=True,
  )
  candidate = NormalizedCandidate(
      company_id="test_001",
      company_name="Example Agency",
      canonical_domain="example.com",
  )
  print(
      f"[*] Starting test crawl for {candidate.canonical_domain}..."
  )
  async with client:
    result = await crawl_domain(candidate, client)
  print(
      f"[+] Crawl complete! Fetched {result.crawl_metadata['pages_fetched']} pages."
  )
  for page in result.pages:
    print(f"  -> [{page.fetch_status}] {page.url} (Status: {page.status_code})")


if __name__ == "__main__":
  asyncio.run(_test_run())