"""
Dedicated Event Handlers for Stripe Webhooks.

Each handler is a focused function receiving the validated, dispatched event payload dict.
Handlers are responsible for:
  - Resolving local model records from Stripe IDs.
  - Creating or updating domain models atomically.
  - Logging meaningful context for observability.
  - Returning silently (not raising) for graceful DoesNotExist cases
    so the dispatcher can record a FAILED status rather than crashing.
"""

import logging, stripe
from decimal import Decimal
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict

from django.utils import timezone

from payments.models import (
    Plan,
    StripeCustomer,
    StripePayment,
    Subscription,
    SubscriptionPeriod,
)
from payments.choices import (
    PaymentStatusChoice,
    PeriodStatusChoice,
    SubscriptionStatusChoice,
)
from payments.services import EntitlementService


logger = logging.getLogger(__name__)

# ==============================================================================
# Helpers
# ==============================================================================

def _to_utc(unix_timestamp: int) -> datetime:
    """Convert a Stripe Unix timestamp integer to a UTC-aware datetime."""
    return datetime.fromtimestamp(unix_timestamp, tz=dt_timezone.utc)


# ==============================================================================
# Stubs 
# ==============================================================================

def handle_checkout_session_completed(event_payload: Dict[str, Any]) -> None:
    """
    Handles 'checkout.session.completed'.

    Fired when a customer successfully completes the hosted Stripe Checkout flow.
    This is the ONLY authoritative trigger for creating a local Subscription record.

    Steps:
      1. Resolve StripeCustomer → Django User from session.customer.
      2. Resolve Plan from session.metadata.django_plan_id.
      3. Retrieve fresh Stripe Subscription object for accurate period dates.
      4. Create or update local Subscription (status, period start/end, started_at).
      5. Open the initial SubscriptionPeriod record.
    """


    session = event_payload.get("data", {}).get("object", {})
    session_id      = session.get("id")
    stripe_cust_id  = session.get("customer")
    stripe_sub_id   = session.get("subscription")
    metadata        = session.get("metadata", {})

    logger.info(
        "Processing checkout.session.completed: session=%s customer=%s subscription=%s",
        session_id, stripe_cust_id, stripe_sub_id,
    )

    # 1. Resolve local user via StripeCustomer
    try:
        stripe_customer = StripeCustomer.objects.select_related("user").get(
            stripe_customer_id=stripe_cust_id
        )
    except StripeCustomer.DoesNotExist:
        logger.error(
            "checkout.session.completed: No StripeCustomer found for stripe_customer_id=%s",
            stripe_cust_id,
        )
        raise ValueError(
            f"No StripeCustomer found for stripe_customer_id={stripe_cust_id!r}"
        )
    user = stripe_customer.user

    # 2. Resolve local Plan from metadata
    plan_id = metadata.get("django_plan_id")
    if not plan_id:
        raise ValueError(
            "checkout.session.completed: metadata missing 'django_plan_id'."
        )
    try:
        plan = Plan.objects.get(pk=int(plan_id))
    except (Plan.DoesNotExist, ValueError, TypeError):
        raise ValueError(
            f"checkout.session.completed: Plan pk={plan_id!r} not found."
        )

    # 3. Fetch fresh Stripe Subscription for accurate period boundaries
    stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
    period_start = _to_utc(stripe_sub["current_period_start"])
    period_end   = _to_utc(stripe_sub["current_period_end"])

    # 4. Create or update local Subscription (idempotent via update_or_create)
    subscription, created = Subscription.objects.update_or_create(
        stripe_subscription_id=stripe_sub_id,
        defaults={
            "user":                 user,
            "plan":               plan,
            "amount":              plan.amount,
            "currency":            plan.currency,
            "billing_interval":    plan.billing_interval,
            "stripe_price_id":     plan.stripe_price_id,
            "status":               stripe_sub["status"],
            "current_period_start": period_start,
            "current_period_end":   period_end,
            "cancel_at_period_end": stripe_sub.get("cancel_at_period_end", False),
        },
    )
    if created:
        subscription.started_at = timezone.now()
        subscription.save(update_fields=["started_at"])

    # 5. Open initial SubscriptionPeriod (idempotent)
    SubscriptionPeriod.objects.get_or_create(
        subscription=subscription,
        start_date=period_start,
        defaults={
            "end_date": period_end,
            "status":   PeriodStatusChoice.ACTIVE,
        },
    )

    logger.info(
        "Subscription %s for user '%s' %s successfully.",
        stripe_sub_id,
        user.username,
        "created" if created else "updated",
    )


def handle_customer_subscription_created(event_payload: Dict[str, Any]) -> None:
    """
    Handles 'customer.subscription.created'.

    Stripe fires this immediately when a subscription object is created, but
    checkout.session.completed is the authoritative event for full provisioning.
    We log here and do nothing else to avoid race conditions.
    """
    subscription = event_payload.get("data", {}).get("object", {})
    logger.info(
        "customer.subscription.created: id=%s status=%s (provisioning deferred to checkout.session.completed)",
        subscription.get("id"),
        subscription.get("status"),
    )


def handle_customer_subscription_updated(event_payload: Dict[str, Any]) -> None:
    """
    Handles 'customer.subscription.updated'.

    Fired on any subscription change:
      - Renewal (new billing period boundaries)
      - Status transition (e.g., past_due → active after retry)
      - Plan upgrade/downgrade (price ID changed)
      - Cancellation flag toggled (cancel_at_period_end)

    Steps:
      1. Locate local Subscription by stripe_subscription_id.
      2. Detect if the billing period has advanced (renewal).
      3. Detect plan change (price ID differs from local record).
      4. Sync all mutating fields.
      5. If period renewed: close old SubscriptionPeriod, open new one.
    """
    stripe_sub      = event_payload.get("data", {}).get("object", {})
    stripe_sub_id   = stripe_sub.get("id")

    try:
        subscription = Subscription.objects.select_related("plan").get(
            stripe_subscription_id=stripe_sub_id
        )
    except Subscription.DoesNotExist:
        # Subscription not yet provisioned locally — skip gracefully.
        logger.warning(
            "customer.subscription.updated: Subscription '%s' not found locally. "
            "This may be normal if checkout.session.completed hasn't fired yet.",
            stripe_sub_id,
        )
        return

    new_period_start = _to_utc(stripe_sub["current_period_start"])
    new_period_end   = _to_utc(stripe_sub["current_period_end"])

    # Detect renewal: period boundaries have advanced
    period_renewed = (
        subscription.current_period_start != new_period_start
        or subscription.current_period_end != new_period_end
    )

    # Detect plan change (upgrade / downgrade via price ID)
    items = stripe_sub.get("items", {}).get("data", [])
    if items:
        new_stripe_price_id = items[0].get("price", {}).get("id")
        if (
            new_stripe_price_id
            and subscription.plan.stripe_price_id != new_stripe_price_id
        ):
            try:
                new_plan = Plan.objects.get(stripe_price_id=new_stripe_price_id)
                subscription.plan = new_plan
                subscription.amount = new_plan.amount
                subscription.currency = new_plan.currency
                subscription.billing_interval = new_plan.billing_interval
                subscription.stripe_price_id = new_plan.stripe_price_id or new_stripe_price_id
                logger.info(
                    "Plan change detected for subscription '%s': new price=%s",
                    stripe_sub_id,
                    new_stripe_price_id,
                )
            except Plan.DoesNotExist:
                logger.warning(
                    "Plan change for subscription '%s': new Stripe price '%s' "
                    "not found locally. Skipping plan update.",
                    stripe_sub_id,
                    new_stripe_price_id,
                )

    # Sync status, period dates, and cancellation flag
    subscription.status               = stripe_sub.get("status", subscription.status)
    subscription.current_period_start = new_period_start
    subscription.current_period_end   = new_period_end
    subscription.cancel_at_period_end = stripe_sub.get("cancel_at_period_end", False)
    subscription.save()

    # Handle period rollover
    if period_renewed:
        closed = SubscriptionPeriod.objects.filter(
            subscription=subscription,
            status=PeriodStatusChoice.ACTIVE,
        ).update(status=PeriodStatusChoice.COMPLETED)

        SubscriptionPeriod.objects.get_or_create(
            subscription=subscription,
            start_date=new_period_start,
            defaults={
                "end_date": new_period_end,
                "status":   PeriodStatusChoice.ACTIVE,
            },
        )
        logger.info(
            "Period renewed for subscription '%s': closed %d old period(s), opened new period.",
            stripe_sub_id,
            closed,
        )

    logger.info(
        "customer.subscription.updated: subscription='%s' status='%s' cancel_at_period_end=%s",
        stripe_sub_id,
        subscription.status,
        subscription.cancel_at_period_end,
    )


def handle_customer_subscription_deleted(event_payload: Dict[str, Any]) -> None:
    """
    Handles 'customer.subscription.deleted'.

    Fired when a subscription is fully terminated:
      - Immediate cancellation was requested (at_period_end=False).
      - cancel_at_period_end was True and the period has now ended.

    Steps:
      1. Locate local Subscription.
      2. Mark status = CANCELED, record canceled_at timestamp.
      3. Close any open SubscriptionPeriod records.
    """
    stripe_sub    = event_payload.get("data", {}).get("object", {})
    stripe_sub_id = stripe_sub.get("id")

    try:
        subscription = Subscription.objects.get(stripe_subscription_id=stripe_sub_id)
    except Subscription.DoesNotExist:
        logger.warning(
            "customer.subscription.deleted: Subscription '%s' not found locally. "
            "Nothing to cancel.",
            stripe_sub_id,
        )
        return

    subscription.status      = SubscriptionStatusChoice.CANCELED
    subscription.canceled_at = timezone.now()
    subscription.save(update_fields=["status", "canceled_at", "updated_at"])

    closed = SubscriptionPeriod.objects.filter(
        subscription=subscription,
        status=PeriodStatusChoice.ACTIVE,
    ).update(status=PeriodStatusChoice.CANCELED)

    logger.info(
        "customer.subscription.deleted: subscription='%s' marked CANCELED. "
        "Closed %d active period(s).",
        stripe_sub_id,
        closed,
    )


def handle_invoice_paid(event_payload: Dict[str, Any]) -> None:
    """Record a successful invoice and open/reset its billing period."""
    invoice = event_payload.get("data", {}).get("object", {})
    invoice_id = invoice.get("id")
    stripe_sub_id = invoice.get("subscription")
    if not invoice_id or not stripe_sub_id:
        logger.info("Ignoring non-subscription invoice.paid event: %s", invoice_id)
        return

    try:
        subscription = Subscription.objects.get(
            stripe_subscription_id=stripe_sub_id
        )
    except Subscription.DoesNotExist:
        logger.warning("invoice.paid: subscription '%s' not found locally", stripe_sub_id)
        return

    lines = invoice.get("lines", {}).get("data", [])
    if lines and lines[0].get("period"):
        period_data = lines[0]["period"]
        period_start = _to_utc(period_data["start"])
        period_end = _to_utc(period_data["end"])
    else:
        period_start = subscription.current_period_start
        period_end = subscription.current_period_end

    if not period_start or not period_end:
        logger.error("invoice.paid: invoice '%s' has no usable billing period", invoice_id)
        raise ValueError(f"Invoice {invoice_id!r} has no usable billing period.")

    current_period = SubscriptionPeriod.objects.current(subscription)
    if (
        current_period
        and int(current_period.start_date.timestamp()) == int(period_start.timestamp())
        and int(current_period.end_date.timestamp()) == int(period_end.timestamp())
    ):
        period, period_created = current_period, False
    else:
        period, period_created = SubscriptionPeriod.objects.get_or_create(
            subscription=subscription,
            start_date=period_start,
            defaults={
                "end_date": period_end,
                "status": PeriodStatusChoice.ACTIVE,
                "stripe_invoice_id": invoice_id,
            },
        )
    StripePayment.objects.get_or_create(
        stripe_invoice_id=invoice_id,
        defaults={
            "subscription": subscription,
            "period": period,
            "stripe_payment_intent_id": invoice.get("payment_intent"),
            "amount": Decimal(invoice.get("amount_paid", 0)) / 100,
            "currency": invoice.get("currency", "usd").lower(),
            "status": PaymentStatusChoice.SUCCEEDED,
            "paid_at": timezone.now(),
        },
    )

    if subscription.status == SubscriptionStatusChoice.PAST_DUE:
        subscription.status = SubscriptionStatusChoice.ACTIVE
        subscription.save(update_fields=["status", "updated_at"])

    if period_created:
        EntitlementService.reset_usage_for_new_period(subscription, period)

    logger.info("invoice.paid: recorded payment for invoice '%s'", invoice_id)


def handle_invoice_payment_failed(event_payload: Dict[str, Any]) -> None:
    """Record a failed invoice and transition its subscription to PAST_DUE."""
    invoice = event_payload.get("data", {}).get("object", {})
    invoice_id = invoice.get("id")
    stripe_sub_id = invoice.get("subscription")
    if not invoice_id or not stripe_sub_id:
        logger.info("Ignoring non-subscription invoice.payment_failed event: %s", invoice_id)
        return

    try:
        subscription = Subscription.objects.get(
            stripe_subscription_id=stripe_sub_id
        )
    except Subscription.DoesNotExist:
        logger.warning("invoice.payment_failed: subscription '%s' not found locally", stripe_sub_id)
        return

    StripePayment.objects.get_or_create(
        stripe_invoice_id=invoice_id,
        defaults={
            "subscription": subscription,
            "period": None,
            "stripe_payment_intent_id": invoice.get("payment_intent"),
            "amount": Decimal(invoice.get("amount_due", 0)) / 100,
            "currency": invoice.get("currency", "usd").lower(),
            "status": PaymentStatusChoice.FAILED,
        },
    )
    if subscription.status not in (
        SubscriptionStatusChoice.PAST_DUE,
        SubscriptionStatusChoice.CANCELED,
    ):
        subscription.status = SubscriptionStatusChoice.PAST_DUE
        subscription.save(update_fields=["status", "updated_at"])
    logger.warning("invoice.payment_failed: recorded failed payment for invoice '%s'", invoice_id)


def handle_invoice_payment_action_required(event_payload: Dict[str, Any]) -> None:
    """
    Handles 'invoice.payment_action_required'.
    Fired when 3D Secure authentication is required from the customer.
    Full implementation is in Phase 9.
    """
    invoice = event_payload.get("data", {}).get("object", {})
    logger.warning(
        "invoice.payment_action_required: id=%s — customer action required",
        invoice.get("id"),
    )


def handle_unhandled_event(event_payload: Dict[str, Any]) -> None:
    """
    Fallback handler for events not currently in the dispatch table.
    """
    event_type = event_payload.get("type", "unknown")
    logger.info("Received unhandled or informational Stripe event: %s", event_type)
