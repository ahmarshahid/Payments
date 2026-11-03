"""
Domain services for feature entitlements and quota enforcement.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import F

from payments.choices import FeatureTypeChoice
from payments.constants import (
    FEATURE_DISABLED_CODE,
    NO_ACTIVE_SUBSCRIPTION_CODE,
    QUOTA_EXCEEDED_CODE,
)
from payments.models import (
    FeatureUsage,
    PlanFeature,
    Subscription,
    SubscriptionPeriod,
)


class QuotaExceededError(Exception):
    """Raised when a quota consumption would exceed the configured limit."""

    def __init__(self, feature_slug: str, used: int, limit: int) -> None:
        self.feature_slug = feature_slug
        self.used = used
        self.limit = limit
        self.code = QUOTA_EXCEEDED_CODE
        super().__init__(
            f"Quota exceeded for '{feature_slug}': {used + 1} > {limit} allowed."
        )


class EntitlementService:
    """Checks feature access and atomically consumes metered quotas."""

    @classmethod
    def _get_active_subscription(cls, user) -> Subscription | None:
        return Subscription.objects.get_active_for_user(user)

    @classmethod
    def _get_plan_feature(
        cls, subscription: Subscription, feature_slug: str
    ) -> PlanFeature | None:
        try:
            return PlanFeature.objects.select_related("feature").get(
                plan=subscription.plan_price.plan,
                feature__slug=feature_slug,
                is_enabled=True,
            )
        except PlanFeature.DoesNotExist:
            return None

    @classmethod
    def check_feature_access(
        cls, user, feature_slug: str, required_amount: int = 1
    ) -> tuple[bool, str]:
        """Return whether the user may access a feature and a reason code."""
        if required_amount <= 0:
            raise ValueError("Required quota amount must be greater than zero.")

        subscription = cls._get_active_subscription(user)
        if not subscription:
            return False, NO_ACTIVE_SUBSCRIPTION_CODE

        plan_feature = cls._get_plan_feature(subscription, feature_slug)
        if not plan_feature:
            return False, FEATURE_DISABLED_CODE

        if plan_feature.feature.type == FeatureTypeChoice.TOGGLE:
            return True, ""

        current_period = SubscriptionPeriod.objects.current(subscription)
        if not current_period:
            return False, NO_ACTIVE_SUBSCRIPTION_CODE

        usage, _ = FeatureUsage.objects.get_or_create(
            subscription=subscription,
            feature=plan_feature.feature,
            period_start=current_period.start_date,
            defaults={"period_end": current_period.end_date},
        )
        if usage.used_amount + required_amount > (plan_feature.limit or 0):
            return False, QUOTA_EXCEEDED_CODE
        return True, ""

    @classmethod
    def consume_quota(cls, user, feature_slug: str, amount: int = 1) -> bool:
        """
        Atomically consume quota for a LIMIT feature.

        The usage row is locked before checking the limit. The final update uses
        an F expression so the increment is performed by the database.
        """
        if amount <= 0:
            raise ValueError("Quota consumption amount must be greater than zero.")

        subscription = cls._get_active_subscription(user)
        if not subscription:
            raise PermissionError(NO_ACTIVE_SUBSCRIPTION_CODE)

        plan_feature = cls._get_plan_feature(subscription, feature_slug)
        if not plan_feature:
            raise PermissionError(FEATURE_DISABLED_CODE)
        if plan_feature.feature.type == FeatureTypeChoice.TOGGLE:
            raise ValueError(
                f"Feature '{feature_slug}' is a TOGGLE type and has no quota."
            )

        current_period = SubscriptionPeriod.objects.current(subscription)
        if not current_period:
            raise PermissionError(NO_ACTIVE_SUBSCRIPTION_CODE)

        with transaction.atomic():
            usage, _ = FeatureUsage.objects.select_for_update().get_or_create(
                subscription=subscription,
                feature=plan_feature.feature,
                period_start=current_period.start_date,
                defaults={"period_end": current_period.end_date},
            )
            limit = plan_feature.limit or 0
            if usage.used_amount + amount > limit:
                raise QuotaExceededError(feature_slug, usage.used_amount, limit)

            FeatureUsage.objects.filter(pk=usage.pk).update(
                used_amount=F("used_amount") + amount
            )

        return True

    @classmethod
    def get_user_usage_summary(cls, user) -> list[dict[str, Any]]:
        """Return current usage and limits for every feature on the user's plan."""
        subscription = cls._get_active_subscription(user)
        if not subscription:
            return []

        plan_features = PlanFeature.objects.filter(
            plan=subscription.plan_price.plan
        ).select_related("feature")
        current_period = SubscriptionPeriod.objects.current(subscription)
        summary: list[dict[str, Any]] = []

        for plan_feature in plan_features:
            entry: dict[str, Any] = {
                "feature_name": plan_feature.feature.name,
                "feature_slug": plan_feature.feature.slug,
                "feature_type": plan_feature.feature.type,
                "is_enabled": plan_feature.is_enabled,
                "limit": plan_feature.limit,
                "unit": plan_feature.unit or "",
                "used_amount": None,
                "remaining": None,
                "percent_used": None,
            }
            if plan_feature.feature.type == FeatureTypeChoice.LIMIT and current_period:
                usage = FeatureUsage.objects.filter(
                    subscription=subscription,
                    feature=plan_feature.feature,
                    period_start=current_period.start_date,
                ).first()
                used = usage.used_amount if usage else 0
                limit = plan_feature.limit or 0
                entry["used_amount"] = used
                entry["remaining"] = max(0, limit - used)
                entry["percent_used"] = round((used / limit * 100), 2) if limit else 0.0
            summary.append(entry)

        return summary

    @classmethod
    def reset_usage_for_new_period(
        cls, subscription: Subscription, new_period: SubscriptionPeriod
    ) -> None:
        """Create zeroed usage records for enabled LIMIT features in a new period."""
        limit_features = PlanFeature.objects.filter(
            plan=subscription.plan_price.plan,
            feature__type=FeatureTypeChoice.LIMIT,
            is_enabled=True,
        ).select_related("feature")

        for plan_feature in limit_features:
            FeatureUsage.objects.get_or_create(
                subscription=subscription,
                feature=plan_feature.feature,
                period_start=new_period.start_date,
                defaults={
                    "period_end": new_period.end_date,
                    "used_amount": 0,
                },
            )
