"""
Automated Tests for Milestone 5: Stripe Checkout Session, Serializers, Views, and API Endpoints.
"""

from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from payments.choices import BillingIntervalChoice, FeatureTypeChoice
from payments.models import Feature, Plan, PlanFeature, PlanPrice, StripeCustomer
from payments.stripe.serializers import CreateCheckoutSessionSerializer

from payments.stripe.services import StripeCheckoutService

User = get_user_model()


class CheckoutSerializerTest(TestCase):
    """Tests for CreateCheckoutSessionSerializer."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Pro Tier", is_active=True)
        self.active_price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("29.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_pro_monthly",
            is_active=True,
        )
        self.inactive_price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("19.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
            is_active=False,
        )

    def test_valid_checkout_serializer(self):
        serializer = CreateCheckoutSessionSerializer(
            data={"plan_price_id": self.active_price.id}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["plan_price_id"], self.active_price)

    def test_nonexistent_plan_price_fails(self):
        serializer = CreateCheckoutSessionSerializer(
            data={"plan_price_id": 99999}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("plan_price_id", serializer.errors)

    def test_inactive_plan_price_fails(self):
        serializer = CreateCheckoutSessionSerializer(
            data={"plan_price_id": self.inactive_price.id}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("plan_price_id", serializer.errors)

    def test_price_on_inactive_plan_fails(self):
        self.plan.is_active = False
        self.plan.save()
        serializer = CreateCheckoutSessionSerializer(
            data={"plan_price_id": self.active_price.id}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("plan_price_id", serializer.errors)


class StripeCheckoutServiceTest(TestCase):
    """Tests for StripeCheckoutService."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            first_name="Alice",
            last_name="Wonder",
        )
        self.plan = Plan.objects.create(name="Starter Tier")
        self.plan_price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("10.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_starter_monthly",
        )

    @patch("stripe.checkout.Session.create")
    @patch("stripe.Customer.create")
    def test_create_subscription_checkout_session(
        self, mock_stripe_customer_create, mock_stripe_session_create
    ):
        # Mock customer creation
        mock_customer = MagicMock()
        mock_customer.id = "cus_alice_123"
        mock_stripe_customer_create.return_value = mock_customer

        # Mock checkout session creation
        mock_session = MagicMock()
        mock_session.id = "cs_test_session_abc"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test_session_abc"
        mock_stripe_session_create.return_value = mock_session

        session = StripeCheckoutService.create_subscription_checkout_session(
            user=self.user,
            plan_price=self.plan_price,
            success_url="https://myapp.com/success/",
            cancel_url="https://myapp.com/cancel/",
        )

        self.assertEqual(session.id, "cs_test_session_abc")
        self.assertEqual(session.url, "https://checkout.stripe.com/pay/cs_test_session_abc")

        # Verify Stripe session arguments
        mock_stripe_session_create.assert_called_once_with(
            mode="subscription",
            customer="cus_alice_123",
            line_items=[{"price": "price_starter_monthly", "quantity": 1}],
            success_url="https://myapp.com/success/",
            cancel_url="https://myapp.com/cancel/",
            subscription_data={
                "metadata": {
                    "django_user_id": str(self.user.id),
                    "django_plan_price_id": str(self.plan_price.id),
                    "django_plan_slug": "starter-tier",
                }
            },
            metadata={
                "django_user_id": str(self.user.id),
                "django_plan_price_id": str(self.plan_price.id),
                "django_plan_slug": "starter-tier",
            },
        )


class CheckoutAPIViewTest(TestCase):
    """Integration tests for checkout views and catalog endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="bob",
            email="bob@example.com",
            password="securepassword123",
        )
        self.plan = Plan.objects.create(name="Pro Tier", is_active=True)
        self.price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("30.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_pro_30",
            is_active=True,
        )

    def test_checkout_requires_authentication(self):
        url = reverse("payments:create-checkout-session")
        response = self.client.post(url, {"plan_price_id": self.price.id})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("payments.stripe.services.StripeCheckoutService.create_subscription_checkout_session")
    def test_authenticated_user_can_create_checkout_session(self, mock_checkout_service):
        mock_session = MagicMock()
        mock_session.id = "cs_test_bob_xyz"
        mock_session.url = "https://checkout.stripe.com/pay/cs_test_bob_xyz"
        mock_checkout_service.return_value = mock_session

        self.client.force_authenticate(user=self.user)
        url = reverse("payments:create-checkout-session")
        response = self.client.post(url, {"plan_price_id": self.price.id})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["session_id"], "cs_test_bob_xyz")
        self.assertEqual(
            response.data["checkout_url"],
            "https://checkout.stripe.com/pay/cs_test_bob_xyz",
        )

    def test_checkout_returns_bad_request_for_invalid_price(self):
        self.client.force_authenticate(user=self.user)
        url = reverse("payments:create-checkout-session")
        response = self.client.post(url, {"plan_price_id": 99999})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_checkout_success_view(self):
        url = reverse("payments:checkout-success")
        response = self.client.get(f"{url}?session_id=cs_test_123")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["session_id"], "cs_test_123")

    def test_checkout_cancel_view(self):
        url = reverse("payments:checkout-cancel")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "canceled")

    def test_plan_list_catalog_api(self):
        url = reverse("payments:plan-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data["results"] if "results" in response.data else response.data), 1)

    def test_plan_detail_catalog_api(self):
        url = reverse("payments:plan-detail", kwargs={"slug": self.plan.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Pro Tier")
