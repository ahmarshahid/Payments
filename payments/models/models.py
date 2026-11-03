"""
Centralized Database Models for Payments and Subscriptions.
Consolidates all domain models in the canonical Django single-models file:
  - Base: TimeStampedModel
  - Catalog: Feature, Plan, PlanFeature
  - Pricing: PlanPrice
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
    PlanPriceManager,
    StripeCustomerManager,
    StripeWebhookEventManager,
    SubscriptionManager,
    SubscriptionPeriodManager,
    FeatureUsageManager,
    StripePaymentManager,
)
from payments.validators import (
    PlanFeatureConfigValidator,
    PlanPriceValidator,
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
        help_text=_("Timestamp when the record was created in the database."),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text=_("Timestamp when the record was last updated in the database."),
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
        help_text=_("Human-readable feature name (e.g., 'API Requests', 'Team Members')."),
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier used for programmatic feature gating in code."),
    )
    description = models.TextField(
        blank=True,
        help_text=_("Detailed description of this feature."),
    )
    type = models.CharField(
        max_length=10,
        choices=FeatureTypeChoice.choices,
        default=FeatureTypeChoice.TOGGLE,
        db_index=True,
        help_text=_("Distinguishes binary toggles from numerical metered limits."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_("Whether this feature is actively available for subscription plans."),
    )

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
        help_text=_("Name of the subscription tier (e.g., 'Pro', 'Enterprise')."),
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Unique programmatic slug identifying the plan tier."),
    )
    description = models.TextField(
        blank=True,
        help_text=_("Overview of what this plan tier includes."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_("Whether this plan is currently available for purchase."),
    )
    features = models.ManyToManyField(
        Feature,
        through="PlanFeature",
        related_name="plans",
        blank=True,
        help_text=_("Features and quotas configured for this plan."),
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
        super().save(*args, **kwargs)


class PlanFeature(TimeStampedModel):
    """
    Custom intermediate model connecting Plan and Feature.
    Maintains plan-specific configurations:
      - For TOGGLE features: enabled/disabled state via is_enabled.
      - For LIMIT features: numerical limits and measurement units.
    """

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="plan_features",
        help_text=_("The plan this feature configuration belongs to."),
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="plan_features",
        help_text=_("The feature being configured."),
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text=_("Controls access for TOGGLE features."),
    )
    limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum allowed quota per billing period for LIMIT features. "
            "Must be left empty for TOGGLE features."
        ),
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=_(
            "Unit of measurement for LIMIT features (e.g., 'requests', 'GB', 'seats')."
        ),
    )

    objects = PlanFeatureManager()

    class Meta:
        verbose_name = _("Plan Feature Configuration")
        verbose_name_plural = _("Plan Feature Configurations")
        unique_together = [("plan", "feature")]
        ordering = ["plan", "feature"]

    def __str__(self) -> str:
        if self.feature.type == FeatureTypeChoice.LIMIT:
            return f"{self.plan.name} - {self.feature.name}: {self.limit} {self.unit}"
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
# 3. Pricing
# ==============================================================================

class PlanPrice(TimeStampedModel):
    """
    Pricing point for a Plan tier.
    Stores the recurring interval, localized currency, amount,
    and maps to an official Stripe recurring Price ID.
    """

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="prices",
        help_text=_("The subscription plan tier this price point belongs to."),
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Price in base currency units (e.g. 20.00 for $20.00)."),
    )
    currency = models.CharField(
        max_length=3,
        default=DEFAULT_CURRENCY,
        help_text=_("3-letter ISO currency code in lowercase (e.g., 'usd', 'eur')."),
    )
    billing_interval = models.CharField(
        max_length=10,
        choices=BillingIntervalChoice.choices,
        default=BillingIntervalChoice.MONTH,
        db_index=True,
        help_text=_("Billing recurrence frequency (e.g., month, year)."),
    )
    stripe_price_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Corresponding Stripe Price ID in sandbox or live (e.g., 'price_1N2...')."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_("Whether this price is actively offered for checkout."),
    )

    objects = PlanPriceManager()

    class Meta:
        verbose_name = _("Plan Price")
        verbose_name_plural = _("Plan Prices")
        ordering = ["plan", "amount"]

    def __str__(self) -> str:
        interval = self.get_billing_interval_display()
        return f"{self.plan.name} - {self.currency.upper()} {self.amount} / {interval}"

    def clean(self) -> None:
        """Centralized model validation."""
        super().clean()
        if self.currency:
            self.currency = self.currency.lower()
        PlanPriceValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        """Enforce validation prior to saving."""
        if self.currency:
            self.currency = self.currency.lower()
        self.full_clean()
        super().save(*args, **kwargs)

    def get_amount_in_cents(self) -> int:
        """
        Converts the decimal amount to smallest currency units (cents)

        as required by the Stripe API.
        """
        return int(round(self.amount * 100))

    @property
    def is_synced_with_stripe(self) -> bool:
        """Returns True if this price point is mapped to a Stripe Price ID."""
        return bool(self.stripe_price_id and self.stripe_price_id.startswith("price_"))


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
        help_text=_("The internal Django user associated with this Stripe customer."),
    )
    stripe_customer_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Stripe Customer ID in test or live mode (e.g., 'cus_12345')."),
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
        db_index=True,
        help_text=_("Unique identifier of the Stripe event (e.g. 'evt_12345')."),
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text=_("The Stripe event type (e.g., 'checkout.session.completed', 'invoice.paid')."),
    )
    status = models.CharField(
        max_length=20,
        choices=WebhookEventStatusChoice.choices,
        default=WebhookEventStatusChoice.PENDING,
        db_index=True,
        help_text=_("Current processing lifecycle status."),
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Full JSON payload received from Stripe."),
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text=_("Traceback or error message if event processing fails."),
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp when the event finished processing."),
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
    and a PlanPrice.  The authoritative source of truth is always Stripe;
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
        help_text=_("The Django user (customer) who owns this subscription."),
    )
    plan_price = models.ForeignKey(
        PlanPrice,
        on_delete=models.PROTECT,  # Never delete a PlanPrice that has live subscriptions.
        related_name="subscriptions",
        help_text=_("The contracted PlanPrice (plan + billing interval + currency)."),
    )
    stripe_subscription_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Stripe Subscription object ID. Format: 'sub_XXXXXXXXXX'."),
    )
    status = models.CharField(
        max_length=25,
        choices=SubscriptionStatusChoice.choices,
        default=SubscriptionStatusChoice.INCOMPLETE,
        db_index=True,
        help_text=_("State machine status, kept in sync with Stripe via webhooks."),
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC timestamp when subscription first became active."),
    )
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC start of the current billing cycle (sourced from Stripe)."),
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC end of the current billing cycle (sourced from Stripe)."),
    )
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text=_(
            "If True, subscription cancels at current_period_end instead of renewing. "
            "Customer retains access until then."
        ),
    )
    canceled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC timestamp when the subscription was formally canceled."),
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
            f"{self.plan_price.plan.name}, {self.status})"
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
        help_text=_("Parent subscription this billing cycle belongs to."),
    )
    start_date = models.DateTimeField(
        help_text=_("UTC start of this billing cycle."),
    )
    end_date = models.DateTimeField(
        help_text=_("UTC end of this billing cycle."),
    )
    status = models.CharField(
        max_length=20,
        choices=PeriodStatusChoice.choices,
        default=PeriodStatusChoice.ACTIVE,
        db_index=True,
        help_text=_("Period status: active (current), completed (renewed), canceled."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID that opened this period. Format: 'in_XXX'."),
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
        help_text=_("The subscription this usage counter belongs to."),
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="usages",
        help_text=_("The metered capability being counted."),
    )
    period_start = models.DateTimeField(
        help_text=_("UTC start of the billing period for which usage is measured."),
    )
    period_end = models.DateTimeField(
        help_text=_("UTC end of the billing period for which usage is measured."),
    )
    used_amount = models.PositiveIntegerField(
        default=0,
        help_text=_("Total units consumed in this billing period."),
    )

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
        help_text=_("The subscription this payment was collected for."),
    )
    period = models.ForeignKey(
        SubscriptionPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        help_text=_("The billing period this payment covers."),
    )
    stripe_payment_intent_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe PaymentIntent ID. Format: 'pi_XXXXXXXXXX'."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID. Format: 'in_XXXXXXXXXX'."),
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_("Amount in standard currency units."),
    )
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
    "PlanPrice",
    "StripeCustomer",
    "StripeWebhookEvent",
    
    "Subscription",
    "SubscriptionPeriod",
    "FeatureUsage",
    "StripePayment",
]
