"""
ADL-SignalForge — Dispatch Layer Initialization
================================================
Exposes production-ready transactional outbox components, cryptographically signed
webhook delivery adapters, and multi-channel alerting notifications (WhatsApp & Email).

Architecture & Design Principles:
- Outbox: Handles transactional event persistence, polling, and row locking (SKIP LOCKED).
- Webhook Client: Stateless transport adapter with HMAC-SHA256 signing & SSRF protection.
- Alert Notifiers: Stateless multi-channel alerting adapters (WhatsApp Cloud API & Resend Email API).
"""

from .outbox import (
    Base,
    EventStatus,
    OutboxEvent,
    create_order_with_outbox,
    process_outbox_batch,
    start_outbox_worker,
)

from .webhook_client import (
    DeliveryResult,
    WebhookClient,
)

from .notifier import (
    BaseNotifier,
    NotificationChannel,
    NotificationResult,
    NotificationStatus,
    ResendEmailNotifier,
    WhatsAppNotifier,
    parse_retry_after,
)

__all__ = [
    # -------------------------------------------------------------------------
    # 1. Transactional Outbox Subsystem (outbox.py)
    # -------------------------------------------------------------------------
    "Base",
    "EventStatus",
    "OutboxEvent",
    "create_order_with_outbox",
    "process_outbox_batch",
    "start_outbox_worker",
    # -------------------------------------------------------------------------
    # 2. Webhook Delivery Subsystem (webhook_client.py)
    # -------------------------------------------------------------------------
    "DeliveryResult",
    "WebhookClient",
    # -------------------------------------------------------------------------
    # 3. Multi-Channel Alert Notifiers (notifier.py)
    # -------------------------------------------------------------------------
    "BaseNotifier",
    "NotificationChannel",
    "NotificationResult",
    "NotificationStatus",
    "ResendEmailNotifier",
    "WhatsAppNotifier",
    "parse_retry_after",
]