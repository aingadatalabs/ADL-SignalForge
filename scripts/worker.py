import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure project root is in path
sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
)

# Import domain modules (conforming to established abstractions)
try:
    from dispatch.outbox import OutboxManager
    from dispatch.webhook_client import WebhookClient
    from dispatch.notifier import WhatsAppNotifier, ResendEmailNotifier
except ImportError as e:
    # Fallback placeholders for module inspection/testing if not fully present
    OutboxManager = None
    WebhookClient = None
    WhatsAppNotifier = None
    ResendEmailNotifier = None

# ------------------------------------------------------------------------------
# Logging & Telemetry Setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signalforge.worker")

WORKER_ID = os.getenv("WORKER_ID", "signalforge-worker-01")
BATCH_LIMIT = int(os.getenv("OUTBOX_BATCH_LIMIT", "25"))
LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "120"))


class OutboxWorkerRunner:
    """
    Coordinates safe, fault-tolerant dispatch of outbox events across 
    webhooks, WhatsApp alerts, and email channels.
    """

    def __init__(self):
        self.worker_id = WORKER_ID
        self.outbox = OutboxManager() if OutboxManager else None
        self.webhook_client = WebhookClient() if WebhookClient else None
        self.whatsapp_notifier = WhatsAppNotifier() if WhatsAppNotifier else None
        self.email_notifier = ResendEmailNotifier() if ResendEmailNotifier else None

    async def execute_batch(self, limit: int = BATCH_LIMIT) -> Dict[str, Any]:
        """
        Claims a bounded batch of deliveries, fans them out to appropriate 
        adapters, and records outcomes independently.
        """
        start_time = time.time()
        metrics = {
            "worker_id": self.worker_id,
            "batch_size": limit,
            "claimed": 0,
            "delivered": 0,
            "retrying": 0,
            "dead_lettered": 0,
            "failed_exceptions": 0,
        }

        if not self.outbox:
            logger.error("OutboxManager is not initialized. Skipping batch execution.")
            return metrics

        # 1. Safely claim eligible deliveries with lease semantics
        try:
            deliveries = self.outbox.claim_batch(
                batch_size=limit,
                worker_id=self.worker_id,
                lease_seconds=LEASE_SECONDS,
            )
        except Exception as e:
            logger.error(f"Failed to claim outbox delivery batch: {e}", exc_info=True)
            return metrics

        metrics["claimed"] = len(deliveries)
        if not deliveries:
            return metrics

        logger.info(f"Claimed {len(deliveries)} outbox deliveries for processing.")

        # 2. Process each delivery with strict exception isolation
        for delivery in deliveries:
            delivery_id = delivery.get("delivery_id", "unknown")
            channel = delivery.get("channel", "").upper()
            
            try:
                logger.debug(f"Processing delivery {delivery_id} via channel {channel}")
                
                # 3. Route to the appropriate adapter
                result = await self._dispatch_to_adapter(delivery)

                # 4. Interpret result and persist outcome via OutboxManager
                if result.get("success"):
                    self.outbox.mark_delivered(
                        delivery_id=delivery_id,
                        response_meta=result.get("metadata", {})
                    )
                    metrics["delivered"] += 1
                else:
                    is_permanent = result.get("is_permanent", False)
                    if is_permanent:
                        self.outbox.mark_dead_letter(
                            delivery_id=delivery_id,
                            reason=result.get("error_message", "Permanent failure")
                        )
                        metrics["dead_lettered"] += 1
                    else:
                        self.outbox.schedule_retry(
                            delivery_id=delivery_id,
                            error_message=result.get("error_message", "Transient failure")
                        )
                        metrics["retrying"] += 1

            except Exception as exc:
                # Guarantee that one failing delivery does not crash the rest of the batch
                metrics["failed_exceptions"] += 1
                logger.error(
                    f"Unexpected exception processing delivery {delivery_id}: {exc}",
                    exc_info=True
                )
                try:
                    self.outbox.schedule_retry(
                        delivery_id=delivery_id,
                        error_message=f"Worker exception: {str(exc)}"
                    )
                except Exception as inner_exc:
                    logger.critical(f"Failed to record retry state for {delivery_id}: {inner_exc}")

        duration_ms = int((time.time() - start_time) * 1000)
        metrics["duration_ms"] = duration_ms
        
        logger.info(
            f"Batch completed in {duration_ms}ms | "
            f"Delivered: {metrics['delivered']}, Retrying: {metrics['retrying']}, "
            f"Dead-Lettered: {metrics['dead_lettered']}, Exceptions: {metrics['failed_exceptions']}"
        )
        return metrics

    async def _dispatch_to_adapter(self, delivery: Dict[str, Any]) -> Dict[str, Any]:
        """Routes delivery payload to the respective client/notifier adapter."""
        channel = delivery.get("channel", "").upper()
        payload = delivery.get("payload", {})

        if channel == "WEBHOOK":
            if not self.webhook_client:
                return {"success": False, "is_permanent": True, "error_message": "WebhookClient missing"}
            return await self.webhook_client.send(
                endpoint_url=delivery.get("destination_url"),
                payload=payload,
                event_id=delivery.get("event_id")
            )

        elif channel == "WHATSAPP":
            if not self.whatsapp_notifier:
                return {"success": False, "is_permanent": True, "error_message": "WhatsAppNotifier missing"}
            return await self.whatsapp_notifier.send(payload)

        elif channel == "EMAIL":
            if not self.email_notifier:
                return {"success": False, "is_permanent": True, "error_message": "ResendEmailNotifier missing"}
            return await self.email_notifier.send(payload)

        else:
            return {
                "success": False,
                "is_permanent": True,
                "error_message": f"Unsupported delivery channel: {channel}"
            }


if __name__ == "__main__":
    runner = OutboxWorkerRunner()
    try:
        asyncio.run(runner.execute_batch())
    except KeyboardInterrupt:
        logger.info("Worker stopped manually.")