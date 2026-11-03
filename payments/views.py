"""
Class-Based Views for Plan Catalog and Subscription Lifecycle Management.
"""
import stripe
from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.models import (
    FeatureUsage,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionPeriod,
    StripePayment,
)
from payments.choices import FeatureTypeChoice
from payments.permissions import HasActiveSubscription
from payments.serializers import (
    CancelSubscriptionSerializer,
    CreateBillingPortalSerializer,
    PlanSerializer,
    StripePaymentSerializer,
    SubscriptionSerializer,
)
from payments.stripe.services import StripePortalService, StripeSubscriptionService


# ==============================================================================
# Catalog Views
# ==============================================================================

class PlanListAPIView(ListAPIView):
    """
    List all active subscription plans with their configured features and prices.
    Publicly accessible endpoint for customers browsing subscription tiers.
    """
    permission_classes = [AllowAny]
    serializer_class   = PlanSerializer
    queryset           = Plan.objects.active().prefetch_related("plan_features__feature")


class PlanDetailAPIView(RetrieveAPIView):
    """
    Retrieve single plan details by slug, including features and prices.
    """
    permission_classes = [AllowAny]
    serializer_class   = PlanSerializer
    lookup_field       = "slug"
    queryset           = Plan.objects.active().prefetch_related("plan_features__feature")


# ==============================================================================
# Subscription Lifecycle Views
# ==============================================================================

class CurrentSubscriptionView(APIView):
    """
    GET /payments/api/subscriptions/current/

    Returns the authenticated user's active or trialing subscription with full
    plan details, billing period, and cancellation flags.
    Returns HTTP 404 if no active subscription exists.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = SubscriptionSerializer(subscription)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CancelSubscriptionView(APIView):
    """
    POST /payments/api/subscriptions/cancel/

    Sends a cancellation request to Stripe for the authenticated user's
    active subscription.

    Request body:
        { "at_period_end": true }   → Cancel at end of billing period (default).
        { "at_period_end": false }  → Cancel immediately.

    Note: The actual status update (cancel_at_period_end=True or status=CANCELED)
    will arrive via the customer.subscription.updated / customer.subscription.deleted
    webhook events. This view returns immediately after the Stripe API call succeeds.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription to cancel."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CancelSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        at_period_end = serializer.validated_data["at_period_end"]

        try:
            StripeSubscriptionService.cancel_subscription(
                subscription, at_period_end=at_period_end
            )
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe API error: {getattr(exc, 'user_message', str(exc))}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "detail": (
                    "Cancellation scheduled for end of billing period."
                    if at_period_end
                    else "Subscription canceled immediately."
                ),
                "at_period_end": at_period_end,
            },
            status=status.HTTP_200_OK,
        )


class ReactivateSubscriptionView(APIView):
    """
    POST /payments/api/subscriptions/reactivate/

    Reverses a pending at-period-end cancellation.
    Only valid if the subscription is still active and cancel_at_period_end=True.

    Sends cancel_at_period_end=False to Stripe.
    The actual DB update arrives via customer.subscription.updated webhook.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not subscription.cancel_at_period_end:
            return Response(
                {"detail": "Subscription is not currently scheduled for cancellation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            StripeSubscriptionService.reactivate_subscription(subscription)
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe API error: {getattr(exc, 'user_message', str(exc))}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {"detail": "Subscription reactivation request sent. It will be active shortly."},
            status=status.HTTP_200_OK,
        )


class UsageSummaryView(APIView):
    """Return the authenticated user's current feature usage summary."""

    authentication_classes = [BasicAuthentication, SessionAuthentication]
    permission_classes = [IsAuthenticated, HasActiveSubscription]

    def get(self, request: Request) -> Response:
        from payments.services import EntitlementService

        summary = EntitlementService.get_user_usage_summary(request.user)
        return Response(summary, status=status.HTTP_200_OK)


class FeatureAccessCheckView(APIView):
    """Check access and remaining quota for one feature."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, feature_slug: str) -> Response:
        from payments.services import EntitlementService

        allowed, reason = EntitlementService.check_feature_access(
            request.user, feature_slug
        )
        remaining = None
        subscription = Subscription.objects.get_active_for_user(request.user)
        if subscription:
            plan_feature = PlanFeature.objects.filter(
                plan=subscription.plan,
                feature__slug=feature_slug,
            ).select_related("feature").first()
            if plan_feature and plan_feature.feature.type == FeatureTypeChoice.LIMIT:
                period = SubscriptionPeriod.objects.current(subscription)
                if period:
                    usage = FeatureUsage.objects.filter(
                        subscription=subscription,
                        feature=plan_feature.feature,
                        period_start=period.start_date,
                    ).first()
                    used = usage.used_amount if usage else 0
                    remaining = max(0, (plan_feature.limit or 0) - used)

        return Response(
            {
                "allowed": allowed,
                "remaining": remaining,
                "reason": reason,
            },
            status=status.HTTP_200_OK,
        )


class BillingHistoryView(ListAPIView):
    """Return the authenticated user's payment ledger."""

    permission_classes = [IsAuthenticated]
    serializer_class = StripePaymentSerializer
    pagination_class = PageNumberPagination

    def get_queryset(self):
        return StripePayment.objects.for_user(self.request.user).order_by("-paid_at")


class CreateBillingPortalSessionView(APIView):
    """Create a hosted Stripe Customer Billing Portal session."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = CreateBillingPortalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            portal_url = StripePortalService.create_portal_session(
                user=request.user,
                return_url=serializer.validated_data.get("return_url"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe API error: {getattr(exc, 'user_message', str(exc))}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"portal_url": portal_url}, status=status.HTTP_200_OK)
