"""
Automated Tests for Milestone 2: Feature, Plan, PlanFeature, and Centralized Validation.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from payments.choices import FeatureTypeChoice
from payments.models import Feature, Plan, PlanFeature
from payments.serializers import PlanFeatureSerializer, PlanSerializer



class FeatureModelTest(TestCase):
    """Tests for the Feature model."""

    def test_feature_creation_and_auto_slug(self):
        feature = Feature.objects.create(
            name="API Access",
            type=FeatureTypeChoice.TOGGLE,
        )
        self.assertEqual(feature.slug, "api-access")
        self.assertEqual(feature.type, FeatureTypeChoice.TOGGLE)
        self.assertTrue(feature.is_active)
        self.assertEqual(str(feature), "API Access (Toggle)")

    def test_feature_manager_filters(self):
        f1 = Feature.objects.create(name="F1", type=FeatureTypeChoice.TOGGLE, is_active=True)
        f2 = Feature.objects.create(name="F2", type=FeatureTypeChoice.LIMIT, is_active=True)
        f3 = Feature.objects.create(name="F3", type=FeatureTypeChoice.TOGGLE, is_active=False)

        self.assertEqual(Feature.objects.active().count(), 2)
        self.assertEqual(Feature.objects.toggles().count(), 2)
        self.assertEqual(Feature.objects.limits().count(), 1)


class PlanAndPlanFeatureTest(TestCase):
    """Tests for Plan, PlanFeature, and centralized validation."""

    def setUp(self):
        self.plan = Plan.objects.create(name="Pro Tier")
        self.toggle_feature = Feature.objects.create(
            name="Export Reports",
            type=FeatureTypeChoice.TOGGLE,
        )
        self.limit_feature = Feature.objects.create(
            name="API Requests",
            type=FeatureTypeChoice.LIMIT,
        )

    def test_plan_auto_slug(self):
        self.assertEqual(self.plan.slug, "pro-tier")
        self.assertEqual(str(self.plan), "Pro Tier")

    def test_valid_toggle_plan_feature(self):
        pf = PlanFeature.objects.create(
            plan=self.plan,
            feature=self.toggle_feature,
            is_enabled=True,
            limit=None,
        )
        self.assertTrue(pf.is_enabled)
        self.assertIn("Enabled", str(pf))

    def test_invalid_toggle_plan_feature_with_limit_raises_validation_error(self):
        """Toggle features must not define numeric limits."""
        with self.assertRaises(ValidationError) as ctx:
            PlanFeature.objects.create(
                plan=self.plan,
                feature=self.toggle_feature,
                is_enabled=True,
                limit=100,  # Invalid for toggle!
            )
        self.assertIn("limit", ctx.exception.message_dict)

    def test_valid_limit_plan_feature(self):
        pf = PlanFeature.objects.create(
            plan=self.plan,
            feature=self.limit_feature,
            is_enabled=True,
            limit=5000,
            unit="requests",
        )
        self.assertEqual(pf.limit, 5000)
        self.assertEqual(pf.unit, "requests")
        self.assertIn("5000 requests", str(pf))

    def test_invalid_limit_plan_feature_missing_limit_raises_validation_error(self):
        """Limit features must specify a numerical limit."""
        with self.assertRaises(ValidationError) as ctx:
            PlanFeature.objects.create(
                plan=self.plan,
                feature=self.limit_feature,
                is_enabled=True,
                limit=None,  # Invalid for limit feature!
                unit="requests",
            )
        self.assertIn("limit", ctx.exception.message_dict)

    def test_invalid_limit_plan_feature_missing_unit_raises_validation_error(self):
        """Limit features must specify a unit."""
        with self.assertRaises(ValidationError) as ctx:
            PlanFeature.objects.create(
                plan=self.plan,
                feature=self.limit_feature,
                is_enabled=True,
                limit=500,
                unit="",  # Invalid for limit feature!
            )
        self.assertIn("unit", ctx.exception.message_dict)

    def test_duplicate_feature_assignment_raises_integrity_error(self):
        """Cannot assign the same feature twice to the same plan."""
        PlanFeature.objects.create(
            plan=self.plan,
            feature=self.toggle_feature,
            is_enabled=True,
        )
        with self.assertRaises((IntegrityError, ValidationError)):
            PlanFeature.objects.create(
                plan=self.plan,
                feature=self.toggle_feature,
                is_enabled=False,
            )

    def test_plan_serializer_with_nested_features(self):
        PlanFeature.objects.create(
            plan=self.plan,
            feature=self.toggle_feature,
            is_enabled=True,
        )
        PlanFeature.objects.create(
            plan=self.plan,
            feature=self.limit_feature,
            is_enabled=True,
            limit=1000,
            unit="requests",
        )

        serializer = PlanSerializer(self.plan)
        data = serializer.data
        self.assertEqual(data["name"], "Pro Tier")
        self.assertEqual(len(data["features"]), 2)
        features_slugs = [f["feature_slug"] for f in data["features"]]
        self.assertIn("export-reports", features_slugs)
        self.assertIn("api-requests", features_slugs)
