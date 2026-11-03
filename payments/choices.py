"""
Centralized Django Model Choices for Payments and Subscriptions.
All models.TextChoices and models.IntegerChoices are defined here.
"""
from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class FeatureTypeChoice(TextChoices):
    """
    Feature type distinguishing binary toggle capabilities from metered limits.
    """
    TOGGLE = "TOGGLE", _("Toggle")
    LIMIT = "LIMIT", _("Limit")


class BillingIntervalChoice(TextChoices):
    """
    Recurring billing intervals supported by Stripe and local catalog.
    """
    DAY = "day", _("Day")
    WEEK = "week", _("Week")
    MONTH = "month", _("Month")
    YEAR = "year", _("Year")


class SubscriptionStatusChoice(TextChoices):
    """
    Subscription state machine aligned with Stripe subscription statuses.
    See: https://stripe.com/docs/billing/subscriptions/overview#subscription-statuses
    """
    INCOMPLETE         = "incomplete",         _("Incomplete")
    INCOMPLETE_EXPIRED = "incomplete_expired",  _("Incomplete Expired")
    TRIALING           = "trialing",            _("Trialing")
    ACTIVE             = "active",              _("Active")
    PAST_DUE           = "past_due",            _("Past Due")
    CANCELED           = "canceled",            _("Canceled")
    UNPAID             = "unpaid",              _("Unpaid")
    PAUSED             = "paused",              _("Paused")


class PaymentStatusChoice(TextChoices):
    """
    Transaction statuses for StripePayment records.
    """
    PENDING = "pending", _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    REFUNDED = "refunded", _("Refunded")


class PeriodStatusChoice(TextChoices):
    """
    Status of an individual billing cycle / period.
    """
    ACTIVE = "active", _("Active")
    COMPLETED = "completed", _("Completed")
    CANCELED = "canceled", _("Canceled")


class WebhookEventStatusChoice(TextChoices):
    """
    Processing lifecycle state of an incoming Stripe webhook event.
    """
    PENDING = "pending", _("Pending")
    PROCESSED = "processed", _("Processed")
    FAILED = "failed", _("Failed")
    IGNORED = "ignored", _("Ignored")
