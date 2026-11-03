"""
Custom Model Managers and QuerySets for Payments and Subscriptions.
Encapsulates domain-level queries cleanly outside models and views.
"""

from typing import Self
from django.db import models
from payments.choices import FeatureTypeChoice, BillingIntervalChoice, WebhookEventStatusChoice, SubscriptionStatusChoice, PeriodStatusChoice

class FeatureQuerySet(models.QuerySet):
    """Custom QuerySet for Feature model."""

    def active(self) -> Self:
        return self.filter(is_active=True)

    def toggles(self) -> Self:
        return self.filter(type=FeatureTypeChoice.TOGGLE)

    def limits(self) -> Self:
        return self.filter(type=FeatureTypeChoice.LIMIT)


class FeatureManager(models.Manager.from_queryset(FeatureQuerySet)):
    """Manager for Feature model providing active and type-filtered access."""
    pass


class PlanQuerySet(models.QuerySet):
    """Custom QuerySet for Plan model."""

    def active(self) -> Self:
        return self.filter(is_active=True)

    def with_features(self) -> Self:
        return self.prefetch_related("plan_features__feature")


class PlanManager(models.Manager.from_queryset(PlanQuerySet)):
    """Manager for Plan model."""
    pass


class PlanFeatureQuerySet(models.QuerySet):
    """Custom QuerySet for PlanFeature through model."""

    def enabled(self) -> Self:
        return self.filter(is_enabled=True)

    def for_plan(self, plan: models.Model) -> Self:
        return self.filter(plan=plan)

    def for_feature(self, feature: models.Model) -> Self:
        return self.filter(feature=feature)


class PlanFeatureManager(models.Manager.from_queryset(PlanFeatureQuerySet)):
    """Manager for PlanFeature model."""
    pass


class StripeCustomerQuerySet(models.QuerySet):
    """Custom QuerySet for StripeCustomer model."""

    def for_user(self, user: models.Model) -> Self:
        return self.filter(user=user)

    def by_stripe_id(self, stripe_customer_id: str) -> Self:
        return self.filter(stripe_customer_id=stripe_customer_id)


class StripeCustomerManager(models.Manager.from_queryset(StripeCustomerQuerySet)):
    """Manager for StripeCustomer model."""
    pass


class StripeWebhookEventQuerySet(models.QuerySet):
    """Custom QuerySet for StripeWebhookEvent model."""

    def processed(self) -> Self:
        return self.filter(status=WebhookEventStatusChoice.PROCESSED)

    def pending(self) -> Self:
        return self.filter(status=WebhookEventStatusChoice.PENDING)

    def failed(self) -> Self:
        return self.filter(status=WebhookEventStatusChoice.FAILED)

    def by_event_id(self, stripe_event_id: str) -> Self:
        return self.filter(stripe_event_id=stripe_event_id)


class StripeWebhookEventManager(models.Manager.from_queryset(StripeWebhookEventQuerySet)):
    """Manager for StripeWebhookEvent model."""
    pass


# ==============================================================================
# Subscription Managers
# ==============================================================================


class SubscriptionQuerySet(models.QuerySet):
    """Custom QuerySet for Subscription model."""

    def active(self) -> "SubscriptionQuerySet":
        """Subscriptions with ACTIVE or TRIALING status."""
        return self.filter(
            status__in=[
                SubscriptionStatusChoice.ACTIVE,
                SubscriptionStatusChoice.TRIALING,
            ]
        )

    def past_due(self) -> "SubscriptionQuerySet":
        """Subscriptions whose latest payment failed; Stripe is retrying."""
        return self.filter(status=SubscriptionStatusChoice.PAST_DUE)

    def canceled(self) -> "SubscriptionQuerySet":
        """Fully terminated subscriptions (historical records)."""
        return self.filter(status=SubscriptionStatusChoice.CANCELED)

    def for_user(self, user: models.Model) -> "SubscriptionQuerySet":
        """All subscriptions belonging to a specific user."""
        return self.filter(user=user)

    def pending_cancellation(self) -> "SubscriptionQuerySet":
        """Active subscriptions scheduled to cancel at period end."""
        return self.active().filter(cancel_at_period_end=True)


class SubscriptionManager(models.Manager):
    """Manager for Subscription model."""

    def get_queryset(self) -> SubscriptionQuerySet:
        return SubscriptionQuerySet(self.model, using=self._db)

    def active(self) -> SubscriptionQuerySet:
        return self.get_queryset().active()

    def past_due(self) -> SubscriptionQuerySet:
        return self.get_queryset().past_due()

    def canceled(self) -> SubscriptionQuerySet:
        return self.get_queryset().canceled()

    def for_user(self, user: models.Model) -> SubscriptionQuerySet:
        return self.get_queryset().for_user(user)

    def get_active_for_user(self, user: models.Model):
        """
        Returns the single ACTIVE or TRIALING subscription for a user,
        with plan pre-selected in one query.
        Returns None if no active subscription exists.
        """
        return (
            self.get_queryset()
            .active()
            .filter(user=user)
            .select_related("plan")
            .first()
        )


class SubscriptionPeriodQuerySet(models.QuerySet):
    """Custom QuerySet for SubscriptionPeriod model."""

    def active(self) -> "SubscriptionPeriodQuerySet":
        return self.filter(status=PeriodStatusChoice.ACTIVE)

    def completed(self) -> "SubscriptionPeriodQuerySet":
        return self.filter(status=PeriodStatusChoice.COMPLETED)

    def for_subscription(self, subscription: models.Model) -> "SubscriptionPeriodQuerySet":
        return self.filter(subscription=subscription)

    def current(self, subscription: models.Model):
        """
        Returns the single currently ACTIVE period for a given subscription,
        or None if no active period exists.
        """
        return self.filter(
            subscription=subscription,
            status=PeriodStatusChoice.ACTIVE,
        ).first()


class SubscriptionPeriodManager(models.Manager):
    """Manager for SubscriptionPeriod model."""

    def get_queryset(self) -> SubscriptionPeriodQuerySet:
        return SubscriptionPeriodQuerySet(self.model, using=self._db)

    def active(self) -> SubscriptionPeriodQuerySet:
        return self.get_queryset().active()

    def for_subscription(self, subscription: models.Model) -> SubscriptionPeriodQuerySet:
        return self.get_queryset().for_subscription(subscription)

    def current(self, subscription: models.Model):
        return self.get_queryset().current(subscription)


class FeatureUsageQuerySet(models.QuerySet):
    """Custom QuerySet for FeatureUsage model."""

    def for_subscription(self, subscription: models.Model) -> "FeatureUsageQuerySet":
        return self.filter(subscription=subscription)

    def for_feature(self, feature_slug: str) -> "FeatureUsageQuerySet":
        return self.filter(feature__slug=feature_slug)

    def current_period(self, subscription: models.Model) -> "FeatureUsageQuerySet":
        """Return usage records for the subscription's active billing period."""
        from payments.models import SubscriptionPeriod

        period = SubscriptionPeriod.objects.current(subscription)
        if not period:
            return self.none()
        return self.filter(
            subscription=subscription,
            period_start=period.start_date,
        )


class FeatureUsageManager(models.Manager):
    """Manager for FeatureUsage model."""

    def get_queryset(self) -> FeatureUsageQuerySet:
        return FeatureUsageQuerySet(self.model, using=self._db)

    def for_subscription(self, subscription: models.Model) -> FeatureUsageQuerySet:
        return self.get_queryset().for_subscription(subscription)

    def current_period(self, subscription: models.Model) -> FeatureUsageQuerySet:
        return self.get_queryset().current_period(subscription)


class StripePaymentQuerySet(models.QuerySet):
    """Custom QuerySet for StripePayment model."""

    def succeeded(self) -> "StripePaymentQuerySet":
        from payments.choices import PaymentStatusChoice
        return self.filter(status=PaymentStatusChoice.SUCCEEDED)

    def failed(self) -> "StripePaymentQuerySet":
        from payments.choices import PaymentStatusChoice
        return self.filter(status=PaymentStatusChoice.FAILED)

    def refunded(self) -> "StripePaymentQuerySet":
        from payments.choices import PaymentStatusChoice
        return self.filter(status=PaymentStatusChoice.REFUNDED)

    def for_subscription(self, subscription: models.Model) -> "StripePaymentQuerySet":
        return self.filter(subscription=subscription)

    def for_user(self, user: models.Model) -> "StripePaymentQuerySet":
        return self.filter(subscription__user=user)


class StripePaymentManager(models.Manager):
    """Manager for StripePayment model."""

    def get_queryset(self) -> StripePaymentQuerySet:
        return StripePaymentQuerySet(self.model, using=self._db)

    def succeeded(self) -> StripePaymentQuerySet:
        return self.get_queryset().succeeded()

    def failed(self) -> StripePaymentQuerySet:
        return self.get_queryset().failed()

    def refunded(self) -> StripePaymentQuerySet:
        return self.get_queryset().refunded()

    def for_subscription(self, subscription: models.Model) -> StripePaymentQuerySet:
        return self.get_queryset().for_subscription(subscription)

    def for_user(self, user: models.Model) -> StripePaymentQuerySet:
        return self.get_queryset().for_user(user).select_related(
            "subscription__plan", "period"
        )
