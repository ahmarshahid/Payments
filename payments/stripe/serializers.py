"""
Serializers for Stripe Checkout and API Interactions.
"""

from rest_framework import serializers
from django.utils.translation import gettext_lazy as _
from payments.models import Plan



class CreateCheckoutSessionSerializer(serializers.Serializer):
    """
    Validates input payload for creating a Stripe Checkout Session.
    """

    plan_id = serializers.IntegerField(required=False)
    plan_price_id = serializers.IntegerField(required=False, help_text=_("Deprecated alias for plan_id."))
    success_url = serializers.URLField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=_("Optional custom success URL after completing checkout."),
    )
    cancel_url = serializers.URLField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=_("Optional custom cancellation URL."),
    )

    def validate(self, attrs):
        value = attrs.get("plan_id", attrs.get("plan_price_id"))
        if value is None:
            raise serializers.ValidationError({"plan_id": _("This field is required.")})
        try:
            plan = Plan.objects.get(id=value)
        except Plan.DoesNotExist:
            raise serializers.ValidationError(_("The requested plan does not exist."))
        if not plan.is_active:
            raise serializers.ValidationError(_("This plan is currently not available."))
        attrs["plan"] = plan
        return attrs

    def validate_plan_price_id(self, value: int) -> int:
        """
        Ensures the plan price exists, is active, and its associated plan is active.
        """
        return value


class CheckoutSessionResponseSerializer(serializers.Serializer):
    """
    Serializes checkout session output.
    """

    session_id = serializers.CharField()
    checkout_url = serializers.URLField()
