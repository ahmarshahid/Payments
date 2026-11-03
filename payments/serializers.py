"""
Class-Based Serializers for Payments and Subscriptions.
Centralizes payload serialization, deserialization, and request-level validation.
"""

from rest_framework import serializers
from payments.models import (
    Feature,
    Plan,
    PlanFeature,
    StripeCustomer,
    StripePayment,
)
from payments.validators import PlanFeatureConfigValidator



class FeatureSerializer(serializers.ModelSerializer):
    """Serializer for Feature model."""

    type_display = serializers.CharField(source="get_type_display", read_only=True)

    class Meta:
        model = Feature
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "type",
            "type_display",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PlanFeatureSerializer(serializers.ModelSerializer):
    """
    Serializer for PlanFeature configuration.
    Validates toggle vs limit rules via PlanFeatureConfigValidator.
    """

    feature_name = serializers.CharField(source="feature.name", read_only=True)
    feature_slug = serializers.CharField(source="feature.slug", read_only=True)
    feature_type = serializers.CharField(source="feature.type", read_only=True)

    class Meta:
        model = PlanFeature
        fields = [
            "id",
            "plan",
            "feature",
            "feature_name",
            "feature_slug",
            "feature_type",
            "is_enabled",
            "limit",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        """Delegate validation to centralized PlanFeatureConfigValidator."""
        class TempPlanFeature:
            feature = attrs.get("feature") or (self.instance.feature if self.instance else None)
            is_enabled = attrs.get("is_enabled", getattr(self.instance, "is_enabled", True))
            limit = attrs.get("limit", getattr(self.instance, "limit", None))

        PlanFeatureConfigValidator.validate(TempPlanFeature())
        return attrs


class PlanSerializer(serializers.ModelSerializer):
    """Public representation of a plan and its current price."""
    features = PlanFeatureSerializer(source="plan_features", many=True, read_only=True)
    amount_in_cents = serializers.IntegerField(source="get_amount_in_cents", read_only=True)
    billing_interval_display = serializers.CharField(source="get_billing_interval_display", read_only=True)
    is_synced = serializers.BooleanField(source="is_synced_with_stripe", read_only=True)

    class Meta:
        model = Plan
        fields = ["id", "name", "slug", "description", "is_active", "amount", "amount_in_cents", "currency", "billing_interval", "billing_interval_display", "stripe_price_id", "is_synced", "features", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at", "amount_in_cents", "is_synced"]
class StripeCustomerSerializer(serializers.ModelSerializer):
    """
    Serializer for StripeCustomer model.
    """

    user_username = serializers.CharField(source="user.username", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = StripeCustomer
        fields = [
            "id",
            "user",
            "user_username",
            "user_email",
            "stripe_customer_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


# ==============================================================================
# Subscription Serializers
# ==============================================================================

class SubscriptionPeriodSerializer(serializers.ModelSerializer):
    """Read-only representation of a single billing period."""

    class Meta:
        from payments.models import SubscriptionPeriod
        model = SubscriptionPeriod
        fields = ["id", "start_date", "end_date", "status", "stripe_invoice_id", "created_at"]
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer):
    """
    Full read-only representation of a Subscription.
    Exposes computed properties and flattened plan/price fields.
    Includes the currently active SubscriptionPeriod as a nested object.
    """

    plan_name        = serializers.CharField(source="plan.name", read_only=True)
    plan_slug        = serializers.CharField(source="plan.slug", read_only=True)
    amount           = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    currency         = serializers.CharField(read_only=True)
    billing_interval = serializers.CharField(read_only=True)
    is_active               = serializers.BooleanField(read_only=True)
    is_past_due             = serializers.BooleanField(read_only=True)
    is_pending_cancellation = serializers.BooleanField(read_only=True)
    current_period          = serializers.SerializerMethodField()

    class Meta:
        from payments.models import Subscription
        model = Subscription
        fields = [
            "id", "stripe_subscription_id", "status",
            "is_active", "is_past_due", "is_pending_cancellation",
            "plan_name", "plan_slug", "amount", "currency", "billing_interval",
            "started_at", "current_period_start", "current_period_end",
            "cancel_at_period_end", "canceled_at", "current_period",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_current_period(self, obj):
        """Return the currently ACTIVE SubscriptionPeriod, or None."""
        from payments.models import SubscriptionPeriod
        period = SubscriptionPeriod.objects.current(obj)
        return SubscriptionPeriodSerializer(period).data if period else None


class CancelSubscriptionSerializer(serializers.Serializer):
    """
    Request body for POST /payments/api/subscriptions/cancel/

    at_period_end=True  (default) → cancel at end of billing period.
    at_period_end=False           → cancel immediately.
    """
    at_period_end = serializers.BooleanField(
        default=True,
        help_text="If true, cancels at period end. If false, cancels immediately.",
    )


class StripePaymentSerializer(serializers.ModelSerializer):
    """Read-only representation of a ledger payment."""

    plan_name = serializers.CharField(
        source="subscription.plan.name", read_only=True
    )
    billing_interval = serializers.CharField(
        source="subscription.billing_interval", read_only=True
    )

    class Meta:
        model = StripePayment
        fields = [
            "id",
            "plan_name",
            "billing_interval",
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            "amount",
            "currency",
            "status",
            "paid_at",
            "created_at",
        ]
        read_only_fields = fields


class CreateBillingPortalSerializer(serializers.Serializer):
    """Request body for the billing portal endpoint."""

    return_url = serializers.URLField(required=False, allow_blank=True)
