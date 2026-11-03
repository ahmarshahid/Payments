"""
Dictionary-Based Stripe Webhook Event Dispatcher.
Routes incoming verified Stripe events to dedicated handler functions
while enforcing strict idempotency via StripeWebhookEvent records.
"""

import logging
from typing import Any, Callable, Dict
from django.db import transaction
from django.utils import timezone
from payments.choices import WebhookEventStatusChoice
from payments.models import StripeWebhookEvent
from payments.stripe.constants import StripeEventType

from payments.stripe.handlers import events

logger = logging.getLogger(__name__)

EventHandlerFunc = Callable[[Dict[str, Any]], None]


class StripeEventDispatcher:
    """
    Class-based dispatcher implementing the Dictionary Dispatch Pattern.
    Maps StripeEventType constant keys directly to handler functions.
    """

    # The Key-Value Event Dispatch Registry
    DISPATCH_TABLE: Dict[str, EventHandlerFunc] = {
        StripeEventType.CHECKOUT_SESSION_COMPLETED: events.handle_checkout_session_completed,
        StripeEventType.CUSTOMER_SUBSCRIPTION_CREATED: events.handle_customer_subscription_created,
        StripeEventType.CUSTOMER_SUBSCRIPTION_UPDATED: events.handle_customer_subscription_updated,
        StripeEventType.CUSTOMER_SUBSCRIPTION_DELETED: events.handle_customer_subscription_deleted,
        StripeEventType.INVOICE_PAID: events.handle_invoice_paid,
        StripeEventType.INVOICE_PAYMENT_FAILED: events.handle_invoice_payment_failed,
        StripeEventType.INVOICE_PAYMENT_ACTION_REQUIRED: events.handle_invoice_payment_action_required,
    }

    @classmethod
    def dispatch(cls, event_payload: Dict[str, Any]) -> StripeWebhookEvent:
        """
        Dispatches a verified Stripe event to its registered handler.
        Guarantees idempotency by checking StripeWebhookEvent audit logs.

        Args:
            event_payload: Verified Stripe event dictionary.

        Returns:
            The processed StripeWebhookEvent instance.
        """
        event_id = event_payload.get("id", "")
        event_type = event_payload.get("type", "unknown")

        # 1. Idempotency Check: Do not re-process already processed events
        existing_event = StripeWebhookEvent.objects.filter(stripe_event_id=event_id).first()
        if existing_event and existing_event.status == WebhookEventStatusChoice.PROCESSED:
            logger.info("Duplicate event ignored (idempotency guard): %s", event_id)
            return existing_event

        # 2. Record or update incoming event as PENDING
        if existing_event:
            event_record = existing_event
            event_record.status = WebhookEventStatusChoice.PENDING
            event_record.error_message = ""
            event_record.save(update_fields=["status", "error_message", "updated_at"])
        else:
            event_record = StripeWebhookEvent.objects.create(
                stripe_event_id=event_id,
                event_type=event_type,
                status=WebhookEventStatusChoice.PENDING,
            )

        # 3. Lookup handler from dictionary dispatch table
        handler = cls.DISPATCH_TABLE.get(event_type, events.handle_unhandled_event)

        # 4. Execute handler with atomic state persistence
        try:
            with transaction.atomic():
                handler(event_payload)
                event_record.status = WebhookEventStatusChoice.PROCESSED
                event_record.processed_at = timezone.now()
                event_record.error_message = ""
                event_record.save(update_fields=["status", "processed_at", "error_message", "updated_at"])

            logger.info("Successfully processed Stripe event '%s' (%s)", event_id, event_type)
            return event_record

        except Exception as e:
            logger.error("Handler failed for event '%s': %s", event_id, str(e), exc_info=True)
            event_record.status = WebhookEventStatusChoice.FAILED
            event_record.error_message = str(e)
            event_record.save(update_fields=["status", "error_message", "updated_at"])
            raise
