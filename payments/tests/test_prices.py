from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.core.exceptions import ValidationError
from django.test import TestCase
from payments.choices import BillingIntervalChoice, FeatureTypeChoice
from payments.models import Feature, Plan, PlanFeature
from payments.serializers import PlanSerializer
from payments.stripe.services import StripeCatalogService

class PlanPricingTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(name='Pro Tier')

    def test_create_valid_plan_pricing(self):
        plan = self.plan
        plan.amount = Decimal('20.00'); plan.currency = 'USD'; plan.billing_interval = 'month'; plan.stripe_price_id = 'price_test_12345'; plan.save()
        plan.refresh_from_db()
        self.assertEqual(plan.currency, 'usd')
        self.assertEqual(plan.get_amount_in_cents(), 2000)
        self.assertTrue(plan.is_synced_with_stripe)

    def test_cents_conversion_accuracy(self):
        self.plan.amount = Decimal('199.99'); self.plan.save()
        self.assertEqual(self.plan.get_amount_in_cents(), 19999)
        self.plan.amount = Decimal('0.00'); self.plan.save()
        self.assertEqual(self.plan.get_amount_in_cents(), 0)

    def test_pricing_validation(self):
        for field, value in [('amount', Decimal('-5.00')), ('currency', 'invalid_currency'), ('billing_interval', 'hourly'), ('stripe_price_id', 'invalid_id')]:
            plan = Plan(name=f'Invalid {field}', **{field: value})
            with self.assertRaises(ValidationError) as ctx: plan.full_clean()
            self.assertIn(field, ctx.exception.message_dict)

    def test_plan_serializer_includes_pricing_and_features(self):
        self.plan.amount = Decimal('49.00'); self.plan.stripe_price_id = 'price_business_monthly'; self.plan.save()
        feature = Feature.objects.create(name='Storage', type=FeatureTypeChoice.LIMIT)
        PlanFeature.objects.create(plan=self.plan, feature=feature, limit=50)
        data = PlanSerializer(self.plan).data
        self.assertEqual(data['amount'], '49.00'); self.assertEqual(data['amount_in_cents'], 4900)
        self.assertEqual(len(data['features']), 1); self.assertEqual(data['stripe_price_id'], 'price_business_monthly')

    @patch('stripe.Product.search')
    @patch('stripe.Product.create')
    @patch('stripe.Price.create')
    def test_sync_plan_creates_product_and_price(self, mock_price_create, mock_product_create, mock_product_search):
        mock_product_search.return_value = MagicMock(data=[])
        mock_product_create.return_value = MagicMock(id='prod_enterprise_123')
        mock_price_create.return_value = MagicMock(id='price_enterprise_monthly_123')
        self.plan.amount = Decimal('500.00'); self.plan.save()
        synced = StripeCatalogService.sync_plan(self.plan)
        self.assertEqual(synced.stripe_price_id, 'price_enterprise_monthly_123')
        mock_price_create.assert_called_once_with(product='prod_enterprise_123', unit_amount=50000, currency='usd', recurring={'interval': 'month'}, metadata={'django_plan_id': str(self.plan.id), 'django_plan_slug': 'pro-tier', 'interval': 'month'})
