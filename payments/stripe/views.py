"""
Class-Based Views for Stripe Checkout and Webhooks.
"""

import logging
from rest_framework import status
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils.translation import gettext_lazy as _
from payments.stripe.serializers import (
    CreateCheckoutSessionSerializer,
    CheckoutSessionResponseSerializer,
)

import stripe
from payments.stripe.constants import STRIPE_SIGNATURE_HEADER
from payments.stripe.handlers.dispatcher import StripeEventDispatcher
from payments.stripe.services import StripeWebhookVerificationService, StripeCheckoutService


logger = logging.getLogger(__name__)


class CreateCheckoutSessionView(APIView):
    """
    Class-Based View to initiate a Stripe Checkout Session in subscription mode.
    Requires user authentication.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs) -> Response:
        serializer = CreateCheckoutSessionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        plan = serializer.validated_data["plan"]
        success_url = serializer.validated_data.get("success_url")
        cancel_url = serializer.validated_data.get("cancel_url")

        try:
            session = StripeCheckoutService.create_subscription_checkout_session(
                user=request.user,
                plan=plan,
                success_url=success_url,
                cancel_url=cancel_url,
            )

            response_serializer = CheckoutSessionResponseSerializer(
                {"session_id": session.id, "checkout_url": session.url}
            )
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error("Checkout creation failed: %s", str(e), exc_info=True)
            return Response(
                {"error": _("Unable to initiate checkout with payment provider. Please try again later.")},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CheckoutSuccessView(APIView):
    """
    Class-Based View handling customer return after completing Stripe Checkout.
    Note: As per architecture rules, subscriptions are NOT activated solely by hitting
    this page; activation is strictly verified via incoming Stripe webhooks.
    """

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs) -> Response:
        session_id = request.query_params.get("session_id")
        return Response(
            {
                "status": "success",
                "message": _("Payment submitted successfully. Your subscription is being activated via secure verification."),
                "session_id": session_id,
            },
            status=status.HTTP_200_OK,
        )


class CheckoutCancelView(APIView):
    """
    Class-Based View handling customer cancellation from the Stripe Checkout page.
    """

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs) -> Response:
        return Response(
            {
                "status": "canceled",
                "message": _("Checkout was canceled. No charges were made."),
            },
            status=status.HTTP_200_OK,
        )


class StripeWebhookView(APIView):
    """
    Class-Based Webhook Endpoint receiving incoming Stripe webhook events.
    Verifies cryptographic signatures and routes events via StripeEventDispatcher.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs) -> Response:

        payload = request.body
        sig_header = request.META.get(STRIPE_SIGNATURE_HEADER, "")

        try:
            event = StripeWebhookVerificationService.verify_and_construct_event(
                payload=payload,
                sig_header=sig_header,
            )
        except stripe.SignatureVerificationError as e:
            logger.warning("Stripe signature verification failed: %s", str(e))
            return Response(
                {"error": "Invalid webhook signature."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as e:
            logger.warning("Stripe webhook payload invalid: %s", str(e))
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Dispatch event to registered handler with idempotency guarantee
        try:
            event_record = StripeEventDispatcher.dispatch(event)
            return Response(
                {
                    "status": "received",
                    "event_id": event.get("id"),
                    "processing_status": event_record.status,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error("Error processing Stripe webhook: %s", str(e), exc_info=True)
            return Response(
                {"error": "Error processing webhook event."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

