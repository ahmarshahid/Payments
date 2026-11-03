"""
Automated Tests for Milestone 6: Webhook Signature Verification,
Idempotency, Dictionary Dispatcher, and StripeWebhookEvent.
"""

from unittest.mock import patch, MagicMock
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
import stripe
from payments.choices import WebhookEventStatusChoice
from payments.models import StripeWebhookEvent
from payments.stripe.constants import StripeEventType, STRIPE_SIGNATURE_HEADER

from payments.stripe.handlers.dispatcher import StripeEventDispatcher
from payments.stripe.services import StripeWebhookVerificationService


class StripeWebhookEventModelTest(TestCase):
    """Tests for StripeWebhookEvent model and validator."""

    def test_create_valid_webhook_event(self):
        event = StripeWebhookEvent.objects.create(
            stripe_event_id="evt_test_123456",
            event_type=StripeEventType.CHECKOUT_SESSION_COMPLETED,
            payload={"id": "evt_test_123456"},
        )
        self.assertEqual(event.status, WebhookEventStatusChoice.PENDING)
        self.assertFalse(event.is_processed)
        self.assertIn("evt_test_123456", str(event))

    def test_invalid_event_id_prefix_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            StripeWebhookEvent.objects.create(
                stripe_event_id="invalid_prefix_123",
                event_type="test.event",
            )
        self.assertIn("stripe_event_id", ctx.exception.message_dict)

    def test_empty_event_id_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            StripeWebhookEvent.objects.create(
                stripe_event_id="",
                event_type="test.event",
            )
        self.assertIn("stripe_event_id", ctx.exception.message_dict)

    def test_webhook_event_manager_filters(self):
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_p1",
            event_type="test",
            status=WebhookEventStatusChoice.PROCESSED,
        )
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_p2",
            event_type="test",
            status=WebhookEventStatusChoice.PENDING,
        )
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_f1",
            event_type="test",
            status=WebhookEventStatusChoice.FAILED,
        )

        self.assertEqual(StripeWebhookEvent.objects.processed().count(), 1)
        self.assertEqual(StripeWebhookEvent.objects.pending().count(), 1)
        self.assertEqual(StripeWebhookEvent.objects.failed().count(), 1)
        self.assertEqual(StripeWebhookEvent.objects.by_event_id("evt_p1").count(), 1)


class StripeWebhookVerificationServiceTest(TestCase):
    """Tests for cryptographic signature verification."""

    @patch("payments.stripe.client.StripeClient.get_webhook_secret")
    def test_missing_webhook_secret_raises_value_error(self, mock_secret):
        mock_secret.return_value = ""
        with self.assertRaises(ValueError):
            StripeWebhookVerificationService.verify_and_construct_event(
                payload=b"{}",
                sig_header="t=123,v1=abc",
            )

    @patch("payments.stripe.client.StripeClient.get_webhook_secret")
    def test_missing_sig_header_raises_value_error(self, mock_secret):
        mock_secret.return_value = "whsec_test_secret"
        with self.assertRaises(ValueError):
            StripeWebhookVerificationService.verify_and_construct_event(
                payload=b"{}",
                sig_header="",
            )

    @patch("stripe.Webhook.construct_event")
    @patch("payments.stripe.client.StripeClient.get_webhook_secret")
    def test_valid_signature_constructs_event(self, mock_secret, mock_construct):
        mock_secret.return_value = "whsec_test_secret"
        mock_construct.return_value = {"id": "evt_test", "type": "invoice.paid"}

        event = StripeWebhookVerificationService.verify_and_construct_event(
            payload=b'{"id":"evt_test"}',
            sig_header="valid_sig_header",
        )
        self.assertEqual(event["id"], "evt_test")
        self.assertEqual(event["type"], "invoice.paid")


class StripeEventDispatcherTest(TestCase):
    """Tests for the Dictionary Dispatcher and Idempotency Guard."""

    def test_dispatch_processes_new_event(self):
        payload = {
            "id": "evt_dispatch_1",
            "type": "customer.created",
            "data": {
                "object": {
                    "id": "cus_123",
                }
            },
        }

        record = StripeEventDispatcher.dispatch(payload)
        self.assertEqual(record.status, WebhookEventStatusChoice.PROCESSED)
        self.assertIsNotNone(record.processed_at)
        self.assertTrue(record.is_processed)

    def test_idempotency_skips_already_processed_event(self):
        payload = {
            "id": "evt_duplicate_test",
            "type": StripeEventType.INVOICE_PAID,
            "data": {"object": {"id": "in_123"}},
        }

        # First dispatch
        first_record = StripeEventDispatcher.dispatch(payload)
        self.assertEqual(first_record.status, WebhookEventStatusChoice.PROCESSED)

        # Mock handler to assert it is NOT called on duplicate dispatch
        with patch.dict(
            StripeEventDispatcher.DISPATCH_TABLE,
            {StripeEventType.INVOICE_PAID: MagicMock()},
        ) as mock_table:
            second_record = StripeEventDispatcher.dispatch(payload)
            mock_table[StripeEventType.INVOICE_PAID].assert_not_called()

        self.assertEqual(first_record.id, second_record.id)
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_duplicate_test").count(), 1)

    def test_dispatch_records_failure_on_exception(self):
        payload = {
            "id": "evt_failure_test",
            "type": StripeEventType.CUSTOMER_SUBSCRIPTION_DELETED,
            "data": {"object": {"id": "sub_123"}},
        }

        def broken_handler(p):
            raise RuntimeError("Database connection glitch")

        with patch.dict(
            StripeEventDispatcher.DISPATCH_TABLE,
            {StripeEventType.CUSTOMER_SUBSCRIPTION_DELETED: broken_handler},
        ):
            with self.assertRaises(RuntimeError):
                StripeEventDispatcher.dispatch(payload)

        record = StripeWebhookEvent.objects.get(stripe_event_id="evt_failure_test")
        self.assertEqual(record.status, WebhookEventStatusChoice.FAILED)
        self.assertIn("Database connection glitch", record.error_message)


class StripeWebhookViewTest(TestCase):
    """Integration tests for StripeWebhookView endpoint."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("payments:stripe-webhook")

    @patch("payments.stripe.services.StripeWebhookVerificationService.verify_and_construct_event")
    def test_valid_webhook_post_returns_200(self, mock_verify):
        mock_verify.return_value = {
            "id": "evt_endpoint_test_1",
            "type": "customer.created",
            "data": {"object": {"id": "cus_abc"}},
        }

        response = self.client.post(
            self.url,
            data=b'{"id":"evt_endpoint_test_1"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="valid_signature",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "received")
        self.assertEqual(response.data["event_id"], "evt_endpoint_test_1")

        # Verify event saved in DB
        self.assertTrue(
            StripeWebhookEvent.objects.filter(stripe_event_id="evt_endpoint_test_1").exists()
        )

    @patch("payments.stripe.services.StripeWebhookVerificationService.verify_and_construct_event")
    def test_invalid_signature_returns_400(self, mock_verify):
        mock_verify.side_effect = stripe.SignatureVerificationError(
            message="Signature verification failed",
            sig_header="bad_sig",
        )

        response = self.client.post(
            self.url,
            data=b'{"id":"evt_endpoint_test_2"}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="bad_signature",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)
