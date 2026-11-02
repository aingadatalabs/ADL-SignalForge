import time
import json
import re
import html
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"


class NotificationStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    AMBIGUOUS_TIMEOUT = "AMBIGUOUS_TIMEOUT"


@dataclass(frozen=True)
class NotificationResult:
    """Structured delivery outcome returned to the outbox/alerting coordinator."""
    status: NotificationStatus
    channel: NotificationChannel
    provider: str
    destination_id: str
    provider_message_id: Optional[str]
    retryable: bool
    retry_after_seconds: Optional[float]
    error_code: Optional[str]
    error_message: Optional[str]
    duration_ms: int

    @property
    def accepted(self) -> bool:
        return self.status in (NotificationStatus.SUBMITTED, NotificationStatus.ACCEPTED)


def parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """
    Parses Retry-After header supporting both delta-seconds (integer/float)
    and HTTP-date formats as specified in RFC 9110.
    """
    if not header_value:
        return None
    
    header_value = header_value.strip()
    # 1. Try parsing as numeric seconds
    try:
        return float(header_value)
    except ValueError:
        pass

    # 2. Try parsing as HTTP-date (e.g., Sun, 06 Nov 2026 08:49:37 GMT)
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(header_value)
        delta = (dt.timestamp() - time.time())
        return max(0.0, delta)
    except Exception:
        return None


class BaseNotifier(ABC):
    """Abstract base class ensuring a unified stateless interface across notification channels."""

    @abstractmethod
    async def send(
        self,
        *,
        destination_id: str,
        recipient: str,
        title: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> NotificationResult:
        pass


class WhatsAppNotifier(BaseNotifier):
    """
    Hardened WhatsApp notification adapter utilizing the Meta Cloud API.
    Stateless transport adapter: handles formatting, signing, transmission, and response classification.
    """

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v18.0",
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
        pool_timeout: float = 2.0,
        max_connections: int = 10,
    ):
        if not access_token or not access_token.strip():
            raise ValueError("WhatsApp access_token is required.")
        if not phone_number_id or not phone_number_id.strip():
            raise ValueError("WhatsApp phone_number_id is required.")

        self.access_token = access_token.strip()
        self.phone_number_id = phone_number_id.strip()
        self.endpoint = f"https://graph.facebook.com/{api_version}/{self.phone_number_id}/messages"

        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        self._limits = httpx.Limits(max_connections=max_connections)
        self._client = httpx.AsyncClient(timeout=self._timeout, limits=self._limits)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _normalize_phone_number(self, phone: str) -> str:
        """Validates and strips phone number for Meta Graph API compliance."""
        cleaned = re.sub(r"[^\d+]", "", phone.strip())
        digits = cleaned[1:] if cleaned.startswith("+") else cleaned
        if not (7 <= len(digits) <= 15):
            raise ValueError(f"Invalid phone number format for WhatsApp: {phone}")
        return digits

    async def send(
        self,
        *,
        destination_id: str,
        recipient: str,
        title: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> NotificationResult:
        start_time = time.perf_counter()

        try:
            normalized_recipient = self._normalize_phone_number(recipient)
        except ValueError as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            return NotificationResult(
                status=NotificationStatus.FAILED,
                channel=NotificationChannel.WHATSAPP,
                provider="meta_cloud_api",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=False,
                retry_after_seconds=None,
                error_code="INVALID_RECIPIENT",
                error_message=str(e),
                duration_ms=duration,
            )

        formatted_body = f"🚨 *{title.strip()}* 🚨\n\n{message.strip()}"
        if metadata:
            meta_snippet = "\n".join([f"• *{k}*: `{v}`" for k, v in metadata.items() if v is not None])
            formatted_body += f"\n\n{meta_snippet}"

        payload = {
            "messaging_product": "whatsapp",
            "to": normalized_recipient,
            "type": "text",
            "text": {"body": formatted_body},
        }

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(self.endpoint, json=payload, headers=headers)
            duration = int((time.perf_counter() - start_time) * 1000)
            retry_after = parse_retry_after(response.headers.get("Retry-After"))

            if 200 <= response.status_code < 300:
                msg_id = None
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("messages", [{}])[0].get("id")
                except Exception:
                    pass  # Defensive parsing: accepted even if JSON ID extraction fails

                logger.info("WhatsApp alert submitted successfully", extra={"destination_id": destination_id, "provider_msg_id": msg_id})
                return NotificationResult(
                    status=NotificationStatus.SUBMITTED,
                    channel=NotificationChannel.WHATSAPP,
                    provider="meta_cloud_api",
                    destination_id=destination_id,
                    provider_message_id=msg_id,
                    retryable=False,
                    retry_after_seconds=None,
                    error_code=None,
                    error_message=None,
                    duration_ms=duration,
                )
            else:
                retryable = response.status_code in (429, 408, 409) or (500 <= response.status_code < 600)
                error_code = f"HTTP_{response.status_code}"
                return NotificationResult(
                    status=NotificationStatus.FAILED,
                    channel=NotificationChannel.WHATSAPP,
                    provider="meta_cloud_api",
                    destination_id=destination_id,
                    provider_message_id=None,
                    retryable=retryable,
                    retry_after_seconds=retry_after,
                    error_code=error_code,
                    error_message=f"API rejected request with status {response.status_code}",
                    duration_ms=duration,
                )

        except httpx.TimeoutException as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            logger.error("WhatsApp notification timed out (Ambiguous outcome)", extra={"destination_id": destination_id})
            return NotificationResult(
                status=NotificationStatus.AMBIGUOUS_TIMEOUT,
                channel=NotificationChannel.WHATSAPP,
                provider="meta_cloud_api",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="TIMEOUT",
                error_message=str(e),
                duration_ms=duration,
            )
        except httpx.RequestError as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            return NotificationResult(
                status=NotificationStatus.FAILED,
                channel=NotificationChannel.WHATSAPP,
                provider="meta_cloud_api",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="NETWORK_ERROR",
                error_message=str(e),
                duration_ms=duration,
            )


class ResendEmailNotifier(BaseNotifier):
    """
    Hardened Email notification adapter using the Resend API relay.
    Enforces strict HTML escaping and defensive JSON parsing.
    """

    def __init__(
        self,
        *,
        api_key: str,
        sender_email: str,
        api_endpoint: str = "https://api.resend.com/emails",
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
        pool_timeout: float = 2.0,
        max_connections: int = 10,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("Email api_key is required.")
        if not sender_email or "@" not in sender_email:
            raise ValueError("Valid sender_email is required.")

        self.api_key = api_key.strip()
        self.sender_email = sender_email.strip()
        self.api_endpoint = api_endpoint.strip()

        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        self._limits = httpx.Limits(max_connections=max_connections)
        self._client = httpx.AsyncClient(timeout=self._timeout, limits=self._limits)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        *,
        destination_id: str,
        recipient: str,
        title: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> NotificationResult:
        start_time = time.perf_counter()
        recipient_clean = recipient.strip()

        if "@" not in recipient_clean:
            duration = int((time.perf_counter() - start_time) * 1000)
            return NotificationResult(
                status=NotificationStatus.FAILED,
                channel=NotificationChannel.EMAIL,
                provider="resend",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=False,
                retry_after_seconds=None,
                error_code="INVALID_RECIPIENT",
                error_message=f"Invalid email address: {recipient}",
                duration_ms=duration,
            )

        safe_title = html.escape(title.strip())
        safe_message = html.escape(message.strip()).replace("\n", "<br>")

        html_metadata = ""
        if metadata:
            rows = []
            for k, v in metadata.items():
                if v is not None:
                    safe_k = html.escape(str(k))
                    safe_v = html.escape(str(v))
                    rows.append(
                        f"<tr><td style='padding: 8px; border: 1px solid #ddd;'><b>{safe_k}</b></td>"
                        f"<td style='padding: 8px; border: 1px solid #ddd;'>{safe_v}</td></tr>"
                    )
            if rows:
                html_metadata = (
                    f"<br><h3>Signal Metadata</h3>"
                    f"<table style='border-collapse: collapse; width: 100%;'>{''.join(rows)}</table>"
                )

        html_content = f"""
        <div style="font-family: Arial, sans-serif; color: #333;">
            <h2 style="color: #d9534f;">[ADL-SignalForge Alert] {safe_title}</h2>
            <p style="font-size: 14px; line-height: 1.5;">{safe_message}</p>
            {html_metadata}
            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="font-size: 11px; color: #777;">Ainga Data Labs Automated Intelligence Pipeline</p>
        </div>
        """

        payload = {
            "from": self.sender_email,
            "to": [recipient_clean],
            "subject": f"[SignalForge Alert] {title.strip()}",
            "html": html_content,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(self.api_endpoint, json=payload, headers=headers)
            duration = int((time.perf_counter() - start_time) * 1000)
            retry_after = parse_retry_after(response.headers.get("Retry-After"))

            if 200 <= response.status_code < 300:
                msg_id = None
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("id") or resp_data.get("message_id")
                except Exception:
                    pass

                logger.info("Email alert submitted successfully", extra={"destination_id": destination_id, "provider_msg_id": msg_id})
                return NotificationResult(
                    status=NotificationStatus.SUBMITTED,
                    channel=NotificationChannel.EMAIL,
                    provider="resend",
                    destination_id=destination_id,
                    provider_message_id=msg_id,
                    retryable=False,
                    retry_after_seconds=None,
                    error_code=None,
                    error_message=None,
                    duration_ms=duration,
                )
            else:
                retryable = response.status_code in (429, 408, 409) or (500 <= response.status_code < 600)
                return NotificationResult(
                    status=NotificationStatus.FAILED,
                    channel=NotificationChannel.EMAIL,
                    provider="resend",
                    destination_id=destination_id,
                    provider_message_id=None,
                    retryable=retryable,
                    retry_after_seconds=retry_after,
                    error_code=f"HTTP_{response.status_code}",
                    error_message=f"API rejected request with status {response.status_code}",
                    duration_ms=duration,
                )

        except httpx.TimeoutException as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            return NotificationResult(
                status=NotificationStatus.AMBIGUOUS_TIMEOUT,
                channel=NotificationChannel.EMAIL,
                provider="resend",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="TIMEOUT",
                error_message=str(e),
                duration_ms=duration,
            )
        except httpx.RequestError as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            return NotificationResult(
                status=NotificationStatus.FAILED,
                channel=NotificationChannel.EMAIL,
                provider="resend",
                destination_id=destination_id,
                provider_message_id=None,
                retryable=True,
                retry_after_seconds=None,
                error_code="NETWORK_ERROR",
                error_message=str(e),
                duration_ms=duration,
            )