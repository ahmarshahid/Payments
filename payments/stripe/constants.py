"""
Stripe Integration Constants.
Contains official Stripe webhook event types, header keys, and integration settings.
"""

from typing import Final

# Stripe Signature Header
STRIPE_SIGNATURE_HEADER: Final[str] = "HTTP_STRIPE_SIGNATURE"

# Default tolerance for webhook timestamp verification (seconds)
DEFAULT_WEBHOOK_TOLERANCE_SECONDS: Final[int] = 300

# Checkout Session Modes
CHECKOUT_MODE_SUBSCRIPTION: Final[str] = "subscription"
CHECKOUT_MODE_PAYMENT: Final[str] = "payment"
CHECKOUT_MODE_SETUP: Final[str] = "setup"

# Supported Webhook Event Types
class StripeEventType:
    CHECKOUT_SESSION_COMPLETED: Final[str] = "checkout.session.completed"
    CUSTOMER_SUBSCRIPTION_CREATED: Final[str] = "customer.subscription.created"
    CUSTOMER_SUBSCRIPTION_UPDATED: Final[str] = "customer.subscription.updated"
    CUSTOMER_SUBSCRIPTION_DELETED: Final[str] = "customer.subscription.deleted"
    INVOICE_PAID: Final[str] = "invoice.paid"
    INVOICE_PAYMENT_FAILED: Final[str] = "invoice.payment_failed"
    INVOICE_PAYMENT_ACTION_REQUIRED: Final[str] = "invoice.payment_action_required"
