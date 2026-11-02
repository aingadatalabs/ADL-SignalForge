import hmac
import hashlib
import time
import json
import ipaddress
import logging
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    """Structured delivery outcome returned to the outbox worker."""
    success: bool
    destination_id: str
    status_code: Optional[int]
    provider_request_id: Optional[str]
    retryable: bool
    retry_after_seconds: Optional[float]
    error_code: Optional[str]
    error_message: Optional[str]
    duration_ms: int


class WebhookClient:
    """
    Stateless-in-behavior delivery adapter for ADL-SignalForge.
    Constructs, signs, transmits, and classifies webhook deliveries without
    managing business persistence, retry states, or destination subscriptions.
    """

    def __init__(
        self,
        *,
        secret: str,
        key_id: str = "key_001",
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
        pool_timeout: float = 2.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        keepalive_expiry: float = 30.0,
    ):
        if not secret or len(secret.strip()) < 16:
            raise ValueError("A secure webhook signing secret (minimum 16 chars) is required.")
        
        self.secret = secret.encode("utf-8")
        self.key_id = key_id

        # Configure granular timeouts and explicit connection pooling limits
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        
        # Instantiate persistent client with redirects disabled to prevent SSRF bypasses
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            limits=self._limits,
            follow_redirects=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        """Gracefully closes the underlying HTTP connection pool."""
        await self._client.aclose()

    def _validate_destination(self, url: str) -> None:
        """
        Enforces strict SSRF prevention policies.
        Rejects HTTP, local/private IP ranges, loopback, and link-local targets.
        """
        parsed = urlparse(url)
        
        if parsed.scheme.lower() != "https":
            raise ValueError(f"Insecure webhook destination rejected (HTTPS required): {url}")
        
        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"Invalid webhook URL hostname: {url}")

        # Check for loopback, localhost, or raw IP violations
        normalized_host = hostname.strip("[]").lower()
        if normalized_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise ValueError(f"Forbidden loopback webhook destination: {url}")

        try:
            ip = ipaddress.ip_address(normalized_host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                raise ValueError(f"Forbidden private/internal IP destination: {url}")
        except ValueError:
            # Hostname is a domain name (e.g., example.com), which is permitted.
            # Production deployments should also enforce network-level egress filtering / DNS rebinding protection.
            pass

    def _generate_signature(self, timestamp: str, payload_bytes: bytes) -> str:
        """Calculates HMAC-SHA256 signature over timestamp and exact body bytes."""
        signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
        signature = hmac.new(self.secret, signed_payload, hashlib.sha256).hexdigest()
        return f"sha256={signature}"

    def _classify_response(self, status_code: int, response_headers: httpx.Headers) -> tuple[bool, bool, Optional[float], str]:
        """
        Classifies HTTP responses into success, retryability, backoff, and error codes.
        """
        retry_after_seconds = None
        raw_retry_after = response_headers.get("Retry-After")
        if raw_retry_after:
            try:
                retry_after_seconds = float(raw_retry_after)
            except ValueError:
                pass

        if 200 <= status_code < 300:
            return True, False, None, "DELIVERED"
        
        # Client errors (4xx) are generally permanent, except specific throttling/rate limits
        if status_code == 429:
            return False, True, retry_after_seconds, "RATE_LIMITED"
        if status_code in (408, 409):
            return False, True, retry_after_seconds, "TIMEOUT_OR_CONFLICT"
        if 400 <= status_code < 500:
            return False, False, None, f"CLIENT_ERROR_{status_code}"
        
        # Server errors (5xx) are temporary and retryable
        if 500 <= status_code < 600:
            return False, True, retry_after_seconds, f"SERVER_ERROR_{status_code}"

        return False, False, None, f"UNKNOWN_STATUS_{status_code}"

    async def send_webhook(
        self,
        *,
        destination_id: str,
        url: str,
        event_id: str,
        event_type: str,
        schema_version: str = "1.0",
        payload: Dict[str, Any],
        execution_id: Optional[str] = None,
        release_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> DeliveryResult:
        """
        Constructs, signs, and dispatches a versioned intelligence event webhook.
        Stateless adapter: does not manage database records, event IDs, or retry counts.
        """
        start_time = time.perf_counter()

        try:
            self._validate_destination(url)
        except ValueError as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            logger.error("Webhook destination validation failed", extra={"destination_id": destination_id, "error": str(e)})
            return DeliveryResult(
                success=False,
                destination_id=destination_id,
                status_code=None,
                provider_request_id=None,
                retryable=False,
                retry_after_seconds=None,
                error_code="INVALID_DESTINATION",
                error_message=str(e),
                duration_ms=duration,
            )

        timestamp = str(int(time.time()))
        occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Versioned integration envelope contract
        envelope: Dict[str, Any] = {
            "event_id": event_id,
            "event_type": event_type,
            "schema_version": schema_version,
            "occurred_at": occurred_at,
            "execution_id": execution_id,
            "release_id": release_id,
            "correlation_id": correlation_id,
            "data": payload,
        }

        # Deterministic byte serialization
        try:
            payload_bytes = json.dumps(
                envelope,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            return DeliveryResult(
                success=False,
                destination_id=destination_id,
                status_code=None,
                provider_request_id=None,
                retryable=False,
                retry_after_seconds=None,
                error_code="SERIALIZATION_ERROR",
                error_message=str(e),
                duration_ms=duration,
            )

        signature = self._generate_signature(timestamp, payload_bytes)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Event-Id": event_id,
            "X-Webhook-Schema-Version": schema_version,
            "X-Webhook-Key-Id": self.key_id,
            "X-Hub-Signature-256": signature,
            "User-Agent": "ADL-SignalForge-WebhookClient/2.0",
        }

        try:
            response = await self._client.post(url, content=payload_bytes, headers=headers)
            duration = int((time.perf_counter() - start_time) * 1000)
            
            provider_req_id = response.headers.get("X-Request-Id") or response.headers.get("X-Correlation-Id")
            success, retryable, retry_after, error_code = self._classify_response(response.status_code, response.headers)

            if success:
                logger.info(
                    "Webhook delivered successfully",
                    extra={
                        "event_id": event_id,
                        "destination_id": destination_id,
                        "status_code": response.status_code,
                        "duration_ms": duration,
                    },
                )
            else:
                logger.warning(
                    "Webhook delivery rejected or failed",
                    extra={
                        "event_id": event_id,
                        "destination_id": destination_id,
                        "status_code": response.status_code,
                        "error_code": error_code,
                        "duration_ms": duration,
                    },
                )

            return DeliveryResult(
                success=success,
                destination_id=destination_id,
                status_code=response.status_code,
                provider_request_id=provider_req_id,
                retryable=retryable,
                retry_after_seconds=retry_after,
                error_code=error_code if not success else None,
                error_message=None if success else f"HTTP {response.status_code}",
                duration_ms=duration,
            )

        except httpx.TimeoutException as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            logger.error("Webhook delivery timed out", extra={"event_id": event_id, "destination_id": destination_id, "duration_ms": duration})
            return DeliveryResult(
                success=False,
                destination_id=destination_id,
                status_code=None,
                provider_request_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="TIMEOUT",
                error_message=str(e),
                duration_ms=duration,
            )

        except httpx.RequestError as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            logger.error("Network error during webhook delivery", extra={"event_id": event_id, "destination_id": destination_id, "error": str(e)})
            return DeliveryResult(
                success=False,
                destination_id=destination_id,
                status_code=None,
                provider_request_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="NETWORK_ERROR",
                error_message=str(e),
                duration_ms=duration,
            )