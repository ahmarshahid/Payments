"""Reusable DRF permissions for subscription and feature entitlement checks."""

from rest_framework.permissions import BasePermission

from payments.choices import SubscriptionStatusChoice
from payments.models import Subscription
from payments.services import EntitlementService


class HasActiveSubscription(BasePermission):
    """Allow access only to users with an ACTIVE or TRIALING subscription."""

    message = "An active subscription is required to access this resource."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and Subscription.objects.filter(
                user=user,
                status__in=[
                    SubscriptionStatusChoice.ACTIVE,
                    SubscriptionStatusChoice.TRIALING,
                ],
            ).exists()
        )


def HasFeatureEntitlement(feature_slug: str, required_amount: int = 1) -> type:
    """Return a DRF permission class for a specific feature entitlement."""

    class FeatureEntitlementPermission(BasePermission):
        message = (
            f"You do not have access to the '{feature_slug}' feature "
            "or its quota is exhausted."
        )

        def has_permission(self, request, view) -> bool:
            allowed, _ = EntitlementService.check_feature_access(
                request.user, feature_slug, required_amount
            )
            return allowed

    FeatureEntitlementPermission.__name__ = (
        f"HasFeatureEntitlement_{feature_slug.replace('-', '_')}"
    )
    return FeatureEntitlementPermission
