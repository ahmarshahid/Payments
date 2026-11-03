"""
Serializers for Stripe Checkout and API Interactions.
"""

from rest_framework import serializers
from django.utils.translation import gettext_lazy as _
from payments.models import PlanPrice



class CreateCheckoutSessionSerializer(serializers.Serializer):
    """
    Validates input payload for creating a Stripe Checkout Session.
    """

    plan_price_id = serializers.IntegerField(
        required=True,
        help_text=_("The ID of the PlanPrice the user wishes to purchase."),
    )
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

    def validate_plan_price_id(self, value: int) -> PlanPrice:
        """
        Ensures the plan price exists, is active, and its associated plan is active.
        """
        try:
            plan_price = PlanPrice.objects.select_related("plan").get(id=value)
        except PlanPrice.DoesNotExist:
            raise serializers.ValidationError(_("The requested PlanPrice does not exist."))

        if not plan_price.is_active:
            raise serializers.ValidationError(_("This price option is currently not available."))

        if not plan_price.plan.is_active:
            raise serializers.ValidationError(_("The associated subscription plan is inactive."))

        return plan_price


class CheckoutSessionResponseSerializer(serializers.Serializer):
    """
    Serializes checkout session output.
    """

    session_id = serializers.CharField()
    checkout_url = serializers.URLField()
