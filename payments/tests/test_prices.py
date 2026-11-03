"""
Automated Tests for Milestone 3: PlanPrice, Pricing Validation, and Stripe Catalog Sync.
"""

from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.core.exceptions import ValidationError
from django.test import TestCase
from payments.choices import BillingIntervalChoice, FeatureTypeChoice
from payments.models import Feature, Plan, PlanFeature, PlanPrice
from payments.serializers import PlanPriceSerializer, PlanSerializer

from payments.stripe.services import StripeCatalogService


class PlanPriceModelTest(TestCase):
    """Tests for the PlanPrice model and validators."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Pro Tier")

    def test_create_valid_plan_price(self):
        price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("20.00"),
            currency="USD",
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_test_12345",
        )
        self.assertEqual(price.currency, "usd")  # Lowercased
        self.assertEqual(price.get_amount_in_cents(), 2000)
        self.assertTrue(price.is_synced_with_stripe)
        self.assertIn("USD 20.00", str(price))

    def test_cents_conversion_accuracy(self):
        price1 = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("199.99"),
            currency="usd",
        )
        self.assertEqual(price1.get_amount_in_cents(), 19999)

        price2 = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("0.00"),
            currency="usd",
        )
        self.assertEqual(price2.get_amount_in_cents(), 0)

    def test_negative_amount_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanPrice.objects.create(
                plan=self.plan,
                amount=Decimal("-5.00"),
            )
        self.assertIn("amount", ctx.exception.message_dict)

    def test_invalid_currency_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanPrice.objects.create(
                plan=self.plan,
                amount=Decimal("10.00"),
                currency="invalid_currency",
            )
        self.assertIn("currency", ctx.exception.message_dict)

    def test_invalid_billing_interval_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanPrice.objects.create(
                plan=self.plan,
                amount=Decimal("10.00"),
                billing_interval="hourly",
            )
        self.assertIn("billing_interval", ctx.exception.message_dict)

    def test_invalid_stripe_price_id_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            PlanPrice.objects.create(
                plan=self.plan,
                amount=Decimal("10.00"),
                stripe_price_id="invalid_id_without_prefix",
            )
        self.assertIn("stripe_price_id", ctx.exception.message_dict)

    def test_plan_price_manager_filters(self):
        p_monthly = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("20.00"),
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_monthly",
            is_active=True,
        )
        p_yearly = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("200.00"),
            billing_interval=BillingIntervalChoice.YEAR,
            stripe_price_id="price_yearly",
            is_active=True,
        )
        p_inactive = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("15.00"),
            billing_interval=BillingIntervalChoice.MONTH,
            is_active=False,
        )

        self.assertEqual(PlanPrice.objects.active().count(), 2)
        self.assertEqual(PlanPrice.objects.monthly().count(), 2)
        self.assertEqual(PlanPrice.objects.yearly().count(), 1)
        self.assertEqual(PlanPrice.objects.with_stripe_id().count(), 2)
        self.assertEqual(PlanPrice.objects.for_plan(self.plan).count(), 3)


class PlanPriceSerializerTest(TestCase):
    """Tests for PlanPrice and Plan serialization."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Business Tier")
        self.price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("49.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
            stripe_price_id="price_business_monthly",
        )

    def test_plan_price_serializer_output(self):
        serializer = PlanPriceSerializer(self.price)
        data = serializer.data
        self.assertEqual(data["amount"], "49.00")
        self.assertEqual(data["amount_in_cents"], 4900)
        self.assertEqual(data["currency"], "usd")
        self.assertEqual(data["billing_interval"], "month")
        self.assertTrue(data["is_synced"])

    def test_plan_serializer_includes_prices_and_features(self):
        feature = Feature.objects.create(name="Storage", type=FeatureTypeChoice.LIMIT)
        PlanFeature.objects.create(
            plan=self.plan,
            feature=feature,
            limit=50,
            unit="GB",
        )

        serializer = PlanSerializer(self.plan)
        data = serializer.data
        self.assertEqual(len(data["prices"]), 1)
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["prices"][0]["stripe_price_id"], "price_business_monthly")


class StripeCatalogServiceTest(TestCase):
    """Tests for StripeCatalogService with mocked Stripe SDK."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Enterprise Tier")
        self.plan_price = PlanPrice.objects.create(
            plan=self.plan,
            amount=Decimal("500.00"),
            currency="usd",
            billing_interval=BillingIntervalChoice.MONTH,
        )

    @patch("stripe.Product.search")
    @patch("stripe.Product.create")
    @patch("stripe.Price.create")
    def test_sync_plan_price_creates_product_and_price(
        self, mock_price_create, mock_product_create, mock_product_search
    ):
        # Simulate product search finding nothing
        mock_product_search.return_value = MagicMock(data=[])

        # Mock Stripe Product creation
        mock_product = MagicMock()
        mock_product.id = "prod_enterprise_123"
        mock_product_create.return_value = mock_product

        # Mock Stripe Price creation
        mock_price = MagicMock()
        mock_price.id = "price_enterprise_monthly_123"
        mock_price_create.return_value = mock_price

        # Execute sync
        synced_price = StripeCatalogService.sync_plan_price(self.plan_price)

        # Assertions
        self.assertEqual(synced_price.stripe_price_id, "price_enterprise_monthly_123")
        self.plan_price.refresh_from_db()
        self.assertEqual(self.plan_price.stripe_price_id, "price_enterprise_monthly_123")
        self.assertTrue(self.plan_price.is_synced_with_stripe)

        # Verify Stripe SDK calls
        mock_product_create.assert_called_once()
        mock_price_create.assert_called_once_with(
            product="prod_enterprise_123",
            unit_amount=50000,
            currency="usd",
            recurring={"interval": "month"},
            metadata={
                "django_price_id": str(self.plan_price.id),
                "django_plan_slug": "enterprise-tier",
                "interval": "month",
            },
        )
