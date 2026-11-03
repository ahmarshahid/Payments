"""
Centralized Class-Based Validators for Payments and Subscriptions.
Contains domain-level and model-level validation classes.
"""

from typing import Any, Optional
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from payments.choices import FeatureTypeChoice


class PlanFeatureConfigValidator:
    """
    Class-based validator for PlanFeature configurations.
    Enforces business rules distinguishing TOGGLE features from LIMIT features:
      - TOGGLE features: Must NOT define numerical limits; configured strictly via is_enabled.
      - LIMIT features: Must specify a non-negative numerical limit.
    """

    @classmethod
    def validate(cls, plan_feature: Any) -> None:
        """
        Validates the PlanFeature model instance or equivalent data structure.
        
        Args:
            plan_feature: PlanFeature instance or object with feature, is_enabled, and limit.
            
        Raises:
            ValidationError: When configuration contradicts feature type rules.
        """
        errors = {}
        feature = getattr(plan_feature, "feature", None)

        if not feature:
            # Cannot validate without an assigned feature
            return

        feature_type = getattr(feature, "type", None)
        is_enabled = getattr(plan_feature, "is_enabled", False)
        limit = getattr(plan_feature, "limit", None)

        if feature_type == FeatureTypeChoice.TOGGLE:
            if limit is not None:
                errors["limit"] = _(
                    f"Feature '{feature.name}' is a TOGGLE feature. "
                    f"Toggle features cannot have a numerical limit. Please clear the limit field."
                )

        elif feature_type == FeatureTypeChoice.LIMIT:
            if limit is None:
                errors["limit"] = _(
                    f"Feature '{feature.name}' is a LIMIT feature. "
                    f"A numerical usage limit must be specified."
                )
            elif limit < 0:
                errors["limit"] = _(
                    "Usage limit must be a non-negative number."
                )

        if errors:
            raise ValidationError(errors)


class SlugFormatValidator:
    """
    Class-based validator ensuring slugs contain only valid lowercase alphanumeric characters and hyphens.
    """

    @classmethod
    def validate(cls, slug_value: str) -> None:
        import re
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug_value):
            raise ValidationError(
                _("Slug must consist of lowercase letters, numbers, and single hyphens without spaces.")
            )


class PlanValidator:
    """
    Class-based validator for Plan pricing fields.
    Enforces rules on currency formatting, non-negative amounts,
    and valid billing intervals.
    """

    @classmethod
    def validate(cls, plan: Any) -> None:
        from decimal import Decimal
        from payments.choices import BillingIntervalChoice

        errors = {}
        amount = getattr(plan, "amount", None)
        currency = getattr(plan, "currency", "")
        billing_interval = getattr(plan, "billing_interval", "")
        stripe_price_id = getattr(plan, "stripe_price_id", None)

        if amount is None or amount < Decimal("0.00"):
            errors["amount"] = _("Price amount must be greater than or equal to 0.00.")

        if not currency or len(currency) != 3 or not currency.isalpha():
            errors["currency"] = _("Currency must be a valid 3-letter ISO code (e.g. 'usd').")

        if billing_interval not in BillingIntervalChoice.values:
            errors["billing_interval"] = _(
                f"Invalid billing interval '{billing_interval}'. "
                f"Must be one of: {BillingIntervalChoice.values}."
            )

        if stripe_price_id and not (stripe_price_id.startswith("price_") or stripe_price_id.startswith("plan_")):
            errors["stripe_price_id"] = _(
                "Stripe Price ID must start with 'price_' or 'plan_'."
            )

        if errors:
            raise ValidationError(errors)


class StripeCustomerValidator:
    """
    Class-based validator for StripeCustomer model instances.
    Enforces format rules on Stripe Customer IDs.
    """

    @classmethod
    def validate(cls, stripe_customer: Any) -> None:
        errors = {}
        user = getattr(stripe_customer, "user", None)
        stripe_customer_id = getattr(stripe_customer, "stripe_customer_id", "")

        if not user:
            errors["user"] = _("StripeCustomer must be associated with a valid User.")

        if not stripe_customer_id or not stripe_customer_id.strip():
            errors["stripe_customer_id"] = _("Stripe Customer ID is required.")
        elif not stripe_customer_id.startswith("cus_"):
            errors["stripe_customer_id"] = _(
                "Stripe Customer ID must start with the standard 'cus_' prefix."
            )

        if errors:
            raise ValidationError(errors)


class StripeWebhookEventValidator:
    """
    Class-based validator for StripeWebhookEvent model instances.
    Enforces format rules on incoming Stripe Event IDs and event types.
    """

    @classmethod
    def validate(cls, webhook_event: Any) -> None:
        errors = {}
        stripe_event_id = getattr(webhook_event, "stripe_event_id", "")
        event_type = getattr(webhook_event, "event_type", "")

        if not stripe_event_id or not stripe_event_id.strip():
            errors["stripe_event_id"] = _("Stripe Event ID is required.")
        elif not stripe_event_id.startswith("evt_"):
            errors["stripe_event_id"] = _("Stripe Event ID must start with 'evt_'.")

        if not event_type or not event_type.strip():
            errors["event_type"] = _("Event type is required.")

        if errors:
            raise ValidationError(errors)


# ==============================================================================
# Subscription Validators
# ==============================================================================

class SubscriptionValidator:
    """
    Enforces all business rules for the Subscription model.
    Called from Subscription.clean().
    """

    @staticmethod
    def validate(instance: Any) -> None:
        SubscriptionValidator._validate_stripe_id_prefix(instance)
        SubscriptionValidator._validate_single_active_subscription(instance)
        SubscriptionValidator._validate_period_dates(instance)

    @staticmethod
    def _validate_stripe_id_prefix(instance: Any) -> None:
        """Stripe subscription IDs must begin with 'sub_'."""
        stripe_subscription_id = getattr(instance, "stripe_subscription_id", "")
        if stripe_subscription_id and not stripe_subscription_id.startswith("sub_"):
            raise ValidationError(
                {"stripe_subscription_id": _("Stripe subscription ID must begin with 'sub_'.")}
            )

    @staticmethod
    def _validate_single_active_subscription(instance: Any) -> None:
        """
        A user may only have ONE subscription in ACTIVE or TRIALING status.
        Historical canceled/incomplete subscriptions are not restricted.
        """
        from payments.choices import SubscriptionStatusChoice

        active_statuses = [
            SubscriptionStatusChoice.ACTIVE,
            SubscriptionStatusChoice.TRIALING,
        ]
        if instance.status not in active_statuses:
            return  # Non-active statuses cannot conflict.

        # Import here to avoid circular import at module level.
        from payments.models.models import Subscription

        conflict_qs = Subscription.objects.filter(
            user=instance.user,
            status__in=active_statuses,
        )
        if instance.pk:
            conflict_qs = conflict_qs.exclude(pk=instance.pk)

        if conflict_qs.exists():
            raise ValidationError(
                {
                    "status": _(
                        "This user already has an active or trialing subscription. "
                        "Cancel the existing one before activating a new one."
                    )
                }
            )

    @staticmethod
    def _validate_period_dates(instance: Any) -> None:
        """current_period_end must not be before current_period_start."""
        start = getattr(instance, "current_period_start", None)
        end   = getattr(instance, "current_period_end", None)
        if start and end and end < start:
            raise ValidationError(
                {
                    "current_period_end": _(
                        "current_period_end must be greater than or equal to current_period_start."
                    )
                }
            )


class SubscriptionPeriodValidator:
    """
    Enforces all business rules for the SubscriptionPeriod model.
    Called from SubscriptionPeriod.clean().
    """

    @staticmethod
    def validate(instance: Any) -> None:
        SubscriptionPeriodValidator._validate_date_range(instance)

    @staticmethod
    def _validate_date_range(instance: Any) -> None:
        """Period end_date must be strictly after start_date."""
        start = getattr(instance, "start_date", None)
        end   = getattr(instance, "end_date", None)
        if start and end and end <= start:
            raise ValidationError(
                {"end_date": _("Period end_date must be strictly after start_date.")}
            )


class FeatureUsageValidator:
    """Enforces business rules on FeatureUsage records."""

    @staticmethod
    def validate(instance: Any) -> None:
        FeatureUsageValidator._validate_period_order(instance)
        FeatureUsageValidator._validate_limit_feature_type(instance)

    @staticmethod
    def _validate_period_order(instance: Any) -> None:
        start = getattr(instance, "period_start", None)
        end = getattr(instance, "period_end", None)
        if start and end and end <= start:
            raise ValidationError(
                {"period_end": _("period_end must be strictly after period_start.")}
            )

    @staticmethod
    def _validate_limit_feature_type(instance: Any) -> None:
        feature = getattr(instance, "feature", None)
        if feature and feature.type == FeatureTypeChoice.TOGGLE:
            raise ValidationError(
                {
                    "feature": _(
                        "FeatureUsage records are only valid for LIMIT-type features. "
                        "TOGGLE features are gated via PlanFeature.is_enabled."
                    )
                }
            )


class StripePaymentValidator:
    """Enforces financial integrity rules on StripePayment records."""

    @staticmethod
    def validate(instance: Any) -> None:
        from decimal import Decimal
        import re

        amount = getattr(instance, "amount", None)
        currency = getattr(instance, "currency", "")
        payment_intent_id = getattr(instance, "stripe_payment_intent_id", None)
        invoice_id = getattr(instance, "stripe_invoice_id", None)
        errors = {}

        if amount is not None and amount < Decimal("0.00"):
            errors["amount"] = _("Payment amount must be zero or positive.")
        if currency and not re.fullmatch(r"[a-z]{3}", currency):
            errors["currency"] = _(
                "Currency must be a 3-letter lowercase ISO code (e.g. 'usd')."
            )
        if payment_intent_id and not payment_intent_id.startswith("pi_"):
            errors["stripe_payment_intent_id"] = _(
                "Payment Intent ID must begin with 'pi_'."
            )
        if invoice_id and not invoice_id.startswith("in_"):
            errors["stripe_invoice_id"] = _(
                "Invoice ID must begin with 'in_'."
            )
        if errors:
            raise ValidationError(errors)
