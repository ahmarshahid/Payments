from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from django.urls import reverse

from payments.choices import FeatureTypeChoice, PeriodStatusChoice
from payments.constants import (
    FEATURE_DISABLED_CODE,
    QUOTA_EXCEEDED_CODE,
)
from payments.models import (
    Feature,
    FeatureUsage,
    Plan,
    PlanFeature,
    Subscription,
    SubscriptionPeriod,
)
from payments.services import EntitlementService, QuotaExceededError


User = get_user_model()


class UsageTestMixin:
    def setUp(self):
        self.user = User.objects.create_user(
            username="usage-user",
            email="usage@example.com",
            password="password",
        )
        self.plan = Plan.objects.create(name="Usage Plan", slug="usage-plan")
        self.plan.amount = "20.00"
        self.plan.stripe_price_id = "price_usage_month"
        self.plan.save()
        self.subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_usage_001",
            status="active",
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
        )
        self.period = SubscriptionPeriod.objects.create(
            subscription=self.subscription,
            start_date=self.subscription.current_period_start,
            end_date=self.subscription.current_period_end,
            status=PeriodStatusChoice.ACTIVE,
        )

    def add_feature(self, slug, feature_type, *, enabled=True, limit=None):
        feature = Feature.objects.create(
            name=slug.replace("-", " ").title(),
            slug=slug,
            type=feature_type,
        )
        PlanFeature.objects.create(
            plan=self.plan,
            feature=feature,
            is_enabled=enabled,
            limit=limit,
        )
        return feature


class FeatureUsageModelTests(UsageTestMixin, TestCase):
    def test_create_valid_feature_usage(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        usage = FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
        )
        self.assertEqual(usage.used_amount, 0)

    def test_unique_together_prevents_duplicate_usage_record(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
        )
        duplicate = FeatureUsage(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_toggle_feature_rejects_usage_record(self):
        feature = self.add_feature("dashboard", FeatureTypeChoice.TOGGLE, enabled=True)
        usage = FeatureUsage(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
        )
        with self.assertRaises(ValidationError) as context:
            usage.full_clean()
        self.assertIn("feature", context.exception.message_dict)

    def test_period_end_must_be_after_period_start(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        usage = FeatureUsage(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.start_date,
        )
        with self.assertRaises(ValidationError) as context:
            usage.full_clean()
        self.assertIn("period_end", context.exception.message_dict)


class EntitlementServiceTests(UsageTestMixin, TestCase):
    def test_toggle_feature_access_returns_true_when_enabled(self):
        self.add_feature("dashboard", FeatureTypeChoice.TOGGLE, enabled=True)
        self.assertEqual(
            EntitlementService.check_feature_access(self.user, "dashboard"),
            (True, ""),
        )

    def test_toggle_feature_access_returns_false_when_disabled(self):
        self.add_feature("dashboard", FeatureTypeChoice.TOGGLE, enabled=False)
        allowed, reason = EntitlementService.check_feature_access(
            self.user, "dashboard"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, FEATURE_DISABLED_CODE)

    def test_limit_feature_allows_consumption_under_quota(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
            used_amount=50,
        )
        self.assertEqual(
            EntitlementService.check_feature_access(self.user, "api-requests"),
            (True, ""),
        )

    def test_limit_feature_blocks_when_quota_exhausted(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=10)
        FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
            used_amount=10,
        )
        allowed, reason = EntitlementService.check_feature_access(
            self.user, "api-requests"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, QUOTA_EXCEEDED_CODE)

    def test_consume_quota_increments_atomically(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        for _ in range(5):
            self.assertTrue(
                EntitlementService.consume_quota(self.user, "api-requests")
            )
        usage = FeatureUsage.objects.get(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
        )
        self.assertEqual(usage.used_amount, 5)

    def test_consume_quota_raises_at_quota_boundary(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=3)
        FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
            used_amount=3,
        )
        with self.assertRaises(QuotaExceededError):
            EntitlementService.consume_quota(self.user, "api-requests")

    def test_reset_usage_for_new_period_creates_fresh_record(self):
        feature = self.add_feature("api-requests", FeatureTypeChoice.LIMIT, limit=100)
        old_usage = FeatureUsage.objects.create(
            subscription=self.subscription,
            feature=feature,
            period_start=self.period.start_date,
            period_end=self.period.end_date,
            used_amount=80,
        )
        new_period = SubscriptionPeriod.objects.create(
            subscription=self.subscription,
            start_date=self.period.end_date,
            end_date=self.period.end_date + timedelta(days=30),
            status=PeriodStatusChoice.ACTIVE,
        )
        EntitlementService.reset_usage_for_new_period(self.subscription, new_period)
        new_usage = FeatureUsage.objects.get(
            subscription=self.subscription,
            feature=feature,
            period_start=new_period.start_date,
        )
        self.assertEqual(new_usage.used_amount, 0)
        self.assertTrue(FeatureUsage.objects.filter(pk=old_usage.pk).exists())


class UsagePermissionTests(UsageTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_usage_summary_requires_authentication(self):
        response = self.client.get(reverse("payments:usage-summary"))
        self.assertEqual(response.status_code, 401)

    def test_usage_summary_blocks_user_without_subscription(self):
        self.subscription.delete()
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("payments:usage-summary"))
        self.assertEqual(response.status_code, 403)
