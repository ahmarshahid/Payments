"""
Centralized Database Models for Payments and Subscriptions.
Consolidates all domain models in the canonical Django single-models file:
  - Base: TimeStampedModel
  - Catalog: Feature, Plan, PlanFeature
  - Pricing: Plan
  - Customers: StripeCustomer
  - Webhooks & Idempotency: StripeWebhookEvent
  - Phase 7: Subscription, SubscriptionPeriod
  - Phase 8: FeatureUsage
  - Phase 9: StripePayment
"""

from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from payments.choices import (
    BillingIntervalChoice,
    FeatureTypeChoice,
    PeriodStatusChoice,
    PaymentStatusChoice,
    SubscriptionStatusChoice,
    WebhookEventStatusChoice,
)
from payments.constants import DEFAULT_CURRENCY
from payments.managers import (
    FeatureManager,
    PlanFeatureManager,
    PlanManager,
    StripeCustomerManager,
    StripeWebhookEventManager,
    SubscriptionManager,
    SubscriptionPeriodManager,
    FeatureUsageManager,
    StripePaymentManager,
)
from payments.validators import (
    PlanFeatureConfigValidator,
    PlanValidator,
    StripeCustomerValidator,
    StripeWebhookEventValidator,
    SubscriptionValidator,
    SubscriptionPeriodValidator,
    FeatureUsageValidator,
    StripePaymentValidator,
)


# ==============================================================================
# 1. Base Abstract Models
# ==============================================================================

class TimeStampedModel(models.Model):
    """
    An abstract base model that provides self-updating
    created_at and updated_at timestamp fields for all child models.
    """

    created_at = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        abstract = True


# ==============================================================================
# 2. Product Catalog & Entitlements
# ==============================================================================

class Feature(TimeStampedModel):
    """
    Represents an application feature or capability that can be provisioned or metered.
    Supports binary toggles (e.g., API access) and numerical limits (e.g., storage GB).
    """

    name = models.CharField(
        max_length=100,
        unique=True,
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
    )
    description = models.TextField(blank=True)
    type = models.CharField(
        max_length=10,
        choices=FeatureTypeChoice.choices,
        default=FeatureTypeChoice.TOGGLE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)

    objects = FeatureManager()

    class Meta:
        verbose_name = _("Feature")
        verbose_name_plural = _("Features")
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_type_display()})"

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

class Plan(TimeStampedModel):
    """
    Defines a subscription tier (e.g., Free, Pro, Enterprise).
    Features are configured via the custom through model PlanFeature.
    """

    name = models.CharField(
        max_length=100,
        unique=True,
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    billing_interval = models.CharField(
        max_length=10,
        choices=BillingIntervalChoice.choices,
        default=BillingIntervalChoice.MONTH,
        db_index=True,
    )
    stripe_price_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    features = models.ManyToManyField(
        Feature,
        through="PlanFeature",
        related_name="plans",
        blank=True,
    )

    objects = PlanManager()

    class Meta:
        verbose_name = _("Plan")
        verbose_name_plural = _("Plans")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        if self.currency:
            self.currency = self.currency.lower()
        super().save(*args, **kwargs)

    def get_amount_in_cents(self) -> int:
        return int(round(self.amount * 100))

    @property
    def is_synced_with_stripe(self) -> bool:
        return bool(self.stripe_price_id and self.stripe_price_id.startswith("price_"))

    def clean(self) -> None:
        super().clean()
        PlanValidator.validate(self)


class PlanFeature(TimeStampedModel):
    """
    Custom intermediate model connecting Plan and Feature.
    Maintains plan-specific toggle access and numerical limits.
    """

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="plan_features",
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="plan_features",
    )
    is_enabled = models.BooleanField(default=True)
    limit = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    objects = PlanFeatureManager()

    class Meta:
        verbose_name = _("Plan Feature Configuration")
        verbose_name_plural = _("Plan Feature Configurations")
        unique_together = [("plan", "feature")]
        ordering = ["plan", "feature"]

    def __str__(self) -> str:
        if self.feature.type == FeatureTypeChoice.LIMIT:
            return f"{self.plan.name} - {self.feature.name}: {self.limit}"
        state = "Enabled" if self.is_enabled else "Disabled"
        return f"{self.plan.name} - {self.feature.name}: {state}"

    def clean(self) -> None:
        """Enforce centralized validation rule via PlanFeatureConfigValidator."""
        super().clean()
        PlanFeatureConfigValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce full validation before database persistence."""
        self.full_clean()
        super().save(*args, **kwargs)


# ==============================================================================
# 3. Customers
# ==============================================================================


# ==============================================================================
# 4. Customers
# ==============================================================================

class StripeCustomer(TimeStampedModel):
    """
    Maps an internal Django user to an external Stripe Customer record.
    Created during the customer's first checkout session and reused indefinitely.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stripe_customer",
    )
    stripe_customer_id = models.CharField(
        max_length=100,
        unique=True,
    )

    objects = StripeCustomerManager()

    class Meta:
        verbose_name = _("Stripe Customer")
        verbose_name_plural = _("Stripe Customers")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user.username} ({self.stripe_customer_id})"

    def clean(self) -> None:
        """Enforce centralized StripeCustomer validation."""
        super().clean()
        StripeCustomerValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce full validation before database persistence."""
        self.full_clean()
        super().save(*args, **kwargs)


# ==============================================================================
# 5. Webhooks & Idempotency
# ==============================================================================

class StripeWebhookEvent(TimeStampedModel):
    """
    Audit log of an incoming Stripe webhook event.
    Guarantees that each event is processed at most once (idempotency guard).
    """

    stripe_event_id = models.CharField(
        max_length=100,
        unique=True,
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=WebhookEventStatusChoice.choices,
        default=WebhookEventStatusChoice.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    objects = StripeWebhookEventManager()

    class Meta:
        verbose_name = _("Stripe Webhook Event")
        verbose_name_plural = _("Stripe Webhook Events")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.stripe_event_id} - {self.event_type} ({self.get_status_display()})"

    def clean(self) -> None:
        """Enforce centralized validation for WebhookEvent."""
        super().clean()
        StripeWebhookEventValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce full validation before database persistence."""
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_processed(self) -> bool:
        """Returns True if this event was successfully processed."""
        return self.status == WebhookEventStatusChoice.PROCESSED


# ==============================================================================
# 6. Subscriptions & Billing Periods  (Phase 7)
# ==============================================================================

class Subscription(TimeStampedModel):
    """
    Represents an active or historical subscription contract between a customer
    and a Plan.  The authoritative source of truth is always Stripe;
    this model is kept in sync exclusively through cryptographically verified
    webhooks — never from a browser redirect URL.

    State machine (mirrors Stripe):
      incomplete ──► active ──► past_due ──► canceled
                 └──► trialing ─┘         └──► unpaid
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,  # Never delete a Plan that has live subscriptions.
        related_name="subscriptions",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    billing_interval = models.CharField(max_length=10, choices=BillingIntervalChoice.choices)
    stripe_price_id = models.CharField(max_length=100, blank=True, default="")
    stripe_subscription_id = models.CharField(
        max_length=100,
        unique=True,
    )
    status = models.CharField(
        max_length=25,
        choices=SubscriptionStatusChoice.choices,
        default=SubscriptionStatusChoice.INCOMPLETE,
        db_index=True,
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
    )
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    objects = SubscriptionManager()

    class Meta:
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="sub_user_status_idx"),
        ]

    def __str__(self) -> str:
        return (
            f"Subscription({self.user.username}, "
            f"{self.plan.name}, {self.status})"
        )

    def clean(self) -> None:
        """Enforce centralized validation via SubscriptionValidator."""
        super().clean()
        SubscriptionValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce full validation before database persistence."""
        self.full_clean()
        super().save(*args, **kwargs)

    # ── Computed Properties ───────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """True if customer has valid, unrestricted access to their plan features."""
        return self.status in [
            SubscriptionStatusChoice.ACTIVE,
            SubscriptionStatusChoice.TRIALING,
        ]

    @property
    def is_past_due(self) -> bool:
        """True if the latest recurring charge failed and Stripe is retrying."""
        return self.status == SubscriptionStatusChoice.PAST_DUE

    @property
    def is_pending_cancellation(self) -> bool:
        """True if the customer requested cancel-at-period-end (still has access)."""
        return bool(self.cancel_at_period_end and self.is_active)


class SubscriptionPeriod(TimeStampedModel):
    """
    An immutable historical record of a single billing cycle for a Subscription.
    One SubscriptionPeriod is created or updated per renewal (monthly/yearly).
    Used as the anchor for StripePayment records and FeatureUsage period resets.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="periods",
    )
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=PeriodStatusChoice.choices,
        default=PeriodStatusChoice.ACTIVE,
        db_index=True,
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )

    objects = SubscriptionPeriodManager()

    class Meta:
        verbose_name = _("Subscription Period")
        verbose_name_plural = _("Subscription Periods")
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return (
            f"Period({self.subscription.stripe_subscription_id}, "
            f"{self.start_date.date()} → {self.end_date.date()}, {self.status})"
        )

    def clean(self) -> None:
        """Enforce centralized validation via SubscriptionPeriodValidator."""
        super().clean()
        SubscriptionPeriodValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce full validation before database persistence."""
        self.full_clean()
        super().save(*args, **kwargs)


class FeatureUsage(TimeStampedModel):
    """
    Real-time usage counter for a metered (LIMIT-type) feature.

    One record exists per subscription, feature, and billing period. Historical
    records are retained when a new period begins.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="usages",
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="usages",
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    used_amount = models.PositiveIntegerField(default=0)

    objects = FeatureUsageManager()

    class Meta:
        verbose_name = _("Feature Usage")
        verbose_name_plural = _("Feature Usages")
        unique_together = [("subscription", "feature", "period_start")]
        ordering = ["-period_start"]

    def __str__(self) -> str:
        return (
            f"{self.subscription.user.username} | "
            f"{self.feature.name}: {self.used_amount} used"
        )

    def clean(self) -> None:
        super().clean()
        FeatureUsageValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


class StripePayment(TimeStampedModel):
    """Immutable financial ledger entry created from Stripe webhooks."""

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    period = models.ForeignKey(
        SubscriptionPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )
    stripe_payment_intent_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(
        max_length=20,
        choices=PaymentStatusChoice.choices,
        default=PaymentStatusChoice.PENDING,
        db_index=True,
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = StripePaymentManager()

    class Meta:
        verbose_name = _("Stripe Payment")
        verbose_name_plural = _("Stripe Payments")
        ordering = ["-paid_at", "-created_at"]

    def __str__(self) -> str:
        return (
            f"Payment({self.subscription.user.username}, "
            f"{self.amount} {self.currency.upper()}, {self.status})"
        )

    def clean(self) -> None:
        super().clean()
        StripePaymentValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)


__all__ = [
    "TimeStampedModel",
    "Feature",
    "Plan",
    "PlanFeature",

    "StripeCustomer",
    "StripeWebhookEvent",
    
    "Subscription",
    "SubscriptionPeriod",
    "FeatureUsage",
    "StripePayment",
]
