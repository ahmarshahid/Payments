from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from payments.choices import PaymentStatusChoice, PeriodStatusChoice, SubscriptionStatusChoice
from payments.models import (
    Plan,
    PlanPrice,
    StripeCustomer,
    StripePayment,
    Subscription,
    SubscriptionPeriod,
)
from payments.stripe.handlers.events import (
    handle_invoice_paid,
    handle_invoice_payment_failed,
)


User = get_user_model()


class PaymentTestMixin:
    def setUp(self):
        self.user = User.objects.create_user(
            username="payment-user",
            email="payment@example.com",
            password="password",
        )
        self.other_user = User.objects.create_user(
            username="other-payment-user",
            email="other@example.com",
            password="password",
        )
        self.plan = Plan.objects.create(name="Payment Plan", slug="payment-plan")
        self.plan_price = PlanPrice.objects.create(
            plan=self.plan,
            amount="29.00",
            currency="usd",
            billing_interval="month",
            stripe_price_id="price_payment_month",
        )
        now = timezone.now()
        self.subscription = Subscription.objects.create(
            user=self.user,
            plan_price=self.plan_price,
            stripe_subscription_id="sub_payment_001",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
        self.period = SubscriptionPeriod.objects.create(
            subscription=self.subscription,
            start_date=now,
            end_date=now + timedelta(days=30),
            status=PeriodStatusChoice.ACTIVE,
        )

    def payment(self, subscription=None, **kwargs):
        kwargs.setdefault("amount", Decimal("29.00"))
        kwargs.setdefault("currency", "usd")
        return StripePayment.objects.create(
            subscription=subscription or self.subscription,
            **kwargs,
        )


class StripePaymentModelTests(PaymentTestMixin, TestCase):
    def test_create_valid_stripe_payment(self):
        payment = self.payment(
            stripe_invoice_id="in_valid_001",
            status=PaymentStatusChoice.SUCCEEDED,
        )
        self.assertEqual(payment.status, PaymentStatusChoice.SUCCEEDED)

    def test_negative_amount_raises_validation_error(self):
        payment = StripePayment(
            subscription=self.subscription,
            amount=Decimal("-1.00"),
            currency="usd",
        )
        with self.assertRaises(ValidationError) as context:
            payment.full_clean()
        self.assertIn("amount", context.exception.message_dict)

    def test_invalid_currency_code_raises_validation_error(self):
        payment = StripePayment(
            subscription=self.subscription,
            amount=Decimal("29.00"),
            currency="USD",
        )
        with self.assertRaises(ValidationError) as context:
            payment.full_clean()
        self.assertIn("currency", context.exception.message_dict)

    def test_invalid_payment_intent_id_prefix_raises_validation_error(self):
        payment = StripePayment(
            subscription=self.subscription,
            amount=Decimal("29.00"),
            currency="usd",
            stripe_payment_intent_id="INVALID_123",
        )
        with self.assertRaises(ValidationError) as context:
            payment.full_clean()
        self.assertIn("stripe_payment_intent_id", context.exception.message_dict)

    def test_manager_succeeded_returns_only_succeeded_payments(self):
        self.payment(stripe_invoice_id="in_success", status=PaymentStatusChoice.SUCCEEDED)
        self.payment(stripe_invoice_id="in_failed", status=PaymentStatusChoice.FAILED)
        self.payment(stripe_invoice_id="in_pending", status=PaymentStatusChoice.PENDING)
        self.assertEqual(StripePayment.objects.succeeded().count(), 1)

    def test_manager_for_user_returns_only_own_payments(self):
        other_subscription = Subscription.objects.create(
            user=self.other_user,
            plan_price=self.plan_price,
            stripe_subscription_id="sub_payment_002",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=self.subscription.current_period_start,
            current_period_end=self.subscription.current_period_end,
        )
        self.payment(stripe_invoice_id="in_user_a")
        self.payment(
            subscription=other_subscription,
            stripe_invoice_id="in_user_b",
        )
        self.assertEqual(StripePayment.objects.for_user(self.user).count(), 1)


class InvoiceWebhookTests(PaymentTestMixin, TestCase):
    def invoice_payload(self, event_type="invoice.paid", invoice_id="in_webhook_001"):
        return {
            "type": event_type,
            "data": {
                "object": {
                    "id": invoice_id,
                    "subscription": self.subscription.stripe_subscription_id,
                    "payment_intent": "pi_webhook_001",
                    "amount_paid": 2900,
                    "amount_due": 2900,
                    "currency": "usd",
                    "lines": {
                        "data": [{
                            "period": {
                                "start": int(self.period.start_date.timestamp()),
                                "end": int(self.period.end_date.timestamp()),
                            }
                        }]
                    },
                }
            },
        }

    def test_invoice_paid_creates_succeeded_payment_and_links_period(self):
        handle_invoice_paid(self.invoice_payload())
        payment = StripePayment.objects.get(stripe_invoice_id="in_webhook_001")
        self.assertEqual(payment.status, PaymentStatusChoice.SUCCEEDED)
        self.assertEqual(payment.period, self.period)

    def test_invoice_paid_restores_past_due_subscription_to_active(self):
        self.subscription.status = SubscriptionStatusChoice.PAST_DUE
        self.subscription.save()
        handle_invoice_paid(self.invoice_payload())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatusChoice.ACTIVE)

    def test_invoice_payment_failed_creates_failed_payment_and_marks_past_due(self):
        payload = self.invoice_payload(
            event_type="invoice.payment_failed",
            invoice_id="in_failed_webhook",
        )
        handle_invoice_payment_failed(payload)
        payment = StripePayment.objects.get(stripe_invoice_id="in_failed_webhook")
        self.subscription.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatusChoice.FAILED)
        self.assertEqual(self.subscription.status, SubscriptionStatusChoice.PAST_DUE)


class BillingViewTests(PaymentTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        StripeCustomer.objects.create(user=self.user, stripe_customer_id="cus_payment_001")
        self.client = APIClient()

    def test_billing_history_returns_user_payments_only(self):
        self.payment(stripe_invoice_id="in_history_user")
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("payments:billing-history"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["stripe_invoice_id"], "in_history_user")

    @patch("payments.views.StripePortalService.create_portal_session")
    def test_create_billing_portal_returns_portal_url(self, create_portal):
        create_portal.return_value = "https://billing.stripe.com/session/test"
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("payments:billing-portal"),
            {"return_url": "https://example.com/account/"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["portal_url"], create_portal.return_value)
