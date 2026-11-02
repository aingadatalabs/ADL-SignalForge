# ADL-SIGNALFORGE: outbox.py 
import json
import logging
import uuid
import random
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("adl.signalforge.outbox")

class EventStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    DELIVERED = "DELIVERED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELLED = "CANCELLED"

class DeliveryChannel(str, Enum):
    DASHBOARD_WEBHOOK = "dashboard_webhook"
    WHATSAPP_ALERT = "whatsapp_alert"
    EMAIL_ALERT = "email_alert"
    CUSTOMER_WEBHOOK = "customer_webhook"

class OutboxEventPayload(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    aggregate_type: str
    aggregate_id: str
    event_type: str
    schema_version: str = "1.0"
    execution_id: str
    release_id: Optional[str] = None
    occurred_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: Dict[str, Any]

class OutboxManager:
    """
    Manages transactional event recording and decoupled delivery state machine.
    """
    def __init__(self, db_connection_factory):
        self.db_factory = db_connection_factory

    def record_event_and_deliveries(
        self, 
        event_payload: OutboxEventPayload, 
        channels: List[DeliveryChannel],
        session=None
    ) -> str:
        """
        Atomically records the event and spawns delivery stubs for each requested channel 
        within the same transaction boundary.
        """
        # In a real repository pattern, this inserts into outbox_events 
        # and fans out records into outbox_deliveries.
        logger.info(
            f"Recording event {event_payload.event_type} [{event_payload.event_id}] "
            f"with {len(channels)} target channels."
        )
        return event_payload.event_id

    @staticmethod
    def calculate_backoff(retry_count: int, base_delay: int = 5, max_delay: int = 3600) -> datetime:
        """
        Calculates exponential backoff with randomized jitter to prevent thundering herd.
        """
        # Exponential formula with cap
        delay = min(max_delay, base_delay * (2 ** retry_count))
        # Add +/- 20% jitter
        jitter = delay * random.uniform(-0.2, 0.2)
        final_delay = max(1, delay + jitter)
        
        return datetime.now(timezone.utc) + timedelta(seconds=final_delay)

    @staticmethod
    def classify_error(status_code: Optional[int], exception: Optional[Exception]) -> bool:
        """
        Determines if an error is transient (eligible for retry) or fatal (move to DLQ).
        """
        if exception:
            # Network timeouts or connection drops are generally transient
            return True
            
        if status_code:
            if status_code in (429, 500, 502, 503, 504):
                return True # Rate limit or server error -> Retry
            if 400 <= status_code < 500:
                return False # Client error (except 429) -> Fatal / DLQ immediately
                
        return True