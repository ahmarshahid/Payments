"""
Phase 7 Test Suite: Subscriptions & Periods Lifecycle.

Tests cover:
  - Subscription model validation (stripe ID prefix, single-active rule, period dates).
  - SubscriptionPeriod model validation.
  - Computed properties: is_active, is_past_due, is_pending_cancellation.
  - SubscriptionManager queryset methods.
  - Webhook handler: checkout.session.completed → provisions Subscription + Period.
  - Webhook handler: customer.subscription.updated → syncs renewal, plan change.
  - Webhook handler: customer.subscription.deleted → marks CANCELED.
"""

from datetime import datetime, timezone as dt_timezone, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from payments.choices import (
    BillingIntervalChoice,
    PeriodStatusChoice,
    SubscriptionStatusChoice,
)
from payments.models import (
    Feature,
    Plan,
    PlanFeature,
    StripeCustomer,
    Subscription,
    SubscriptionPeriod,
)
from payments.stripe.handlers.events import (
    handle_checkout_session_completed,
    handle_customer_subscription_updated,
    handle_customer_subscription_deleted,
)

User = get_user_model()


# ==============================================================================
# Shared Fixtures
# ==============================================================================

def _make_user(username="testuser", email="test@example.com"):
    return User.objects.create_user(username=username, email=email, password="pass")


def _make_plan(plan_name="Pro", amount="29.00", interval="month"):
    return Plan.objects.create(
        name=plan_name, slug=plan_name.lower(), amount=amount,
        currency="usd", billing_interval=interval,
        stripe_price_id=f"price_{plan_name.lower()}_{interval}",
    )


def _now_plus(days: int):
    return timezone.now() + timedelta(days=days)


def _make_subscription(
    user,
    plan,
    status=SubscriptionStatusChoice.ACTIVE,
    stripe_id=None,
    cancel_at_period_end=False,
):
    from django.db import connection
    # Bypass full_clean for direct DB insertions in setup (avoids one-active rule conflicts)
    sub = Subscription(
        user=user,
        plan=plan,
        amount=plan.amount, currency=plan.currency, billing_interval=plan.billing_interval, stripe_price_id=plan.stripe_price_id,
        stripe_subscription_id=stripe_id or f"sub_test_{user.pk}_{plan.pk}",
        status=status,
        current_period_start=timezone.now(),
        current_period_end=_now_plus(30),
        cancel_at_period_end=cancel_at_period_end,
    )
    # Use update_or_create at DB level to skip full_clean during test setup
    Subscription.objects.filter(
        stripe_subscription_id=sub.stripe_subscription_id
    ).delete()
    sub.save = super(Subscription, sub).save  # Skip full_clean
    super(Subscription, sub).save()
    return Subscription.objects.get(stripe_subscription_id=sub.stripe_subscription_id)


def _make_period(subscription, status=PeriodStatusChoice.ACTIVE, offset_days=0):
    period = SubscriptionPeriod.__new__(SubscriptionPeriod)
    SubscriptionPeriod.__init__(
        period,
        subscription=subscription,
        start_date=timezone.now() + timedelta(days=offset_days),
        end_date=timezone.now() + timedelta(days=offset_days + 30),
        status=status,
    )
    super(SubscriptionPeriod, period).save()
    return period


# ==============================================================================
# Simpler helpers using model.objects.create bypassing save() override
# ==============================================================================

def _create_subscription_raw(user, plan, status=SubscriptionStatusChoice.ACTIVE,
                              stripe_id=None, cancel_at_period_end=False,
                              period_start=None, period_end=None):
    """Create a Subscription directly in the DB, bypassing full_clean."""
    from django.db import connection
    now = timezone.now()
    sub = Subscription.__new__(Subscription)
    Subscription.__init__(
        sub,
        user=user,
        plan=plan,
        amount=plan.amount, currency=plan.currency, billing_interval=plan.billing_interval, stripe_price_id=plan.stripe_price_id,
        stripe_subscription_id=stripe_id or f"sub_{user.pk}_{plan.pk}_{status}",
        status=status,
        current_period_start=period_start or now,
        current_period_end=period_end or (now + timedelta(days=30)),
        cancel_at_period_end=cancel_at_period_end,
    )
    # Call grandparent save() to bypass our full_clean override
    from django.db.models import Model
    Model.save(sub)
    return Subscription.objects.get(stripe_subscription_id=sub.stripe_subscription_id)


def _create_period_raw(subscription, status=PeriodStatusChoice.ACTIVE, offset_days=0):
    """Create a SubscriptionPeriod directly in the DB, bypassing full_clean."""
    from django.db.models import Model
    now = timezone.now()
    p = SubscriptionPeriod.__new__(SubscriptionPeriod)
    SubscriptionPeriod.__init__(
        p,
        subscription=subscription,
        start_date=now + timedelta(days=offset_days),
        end_date=now + timedelta(days=offset_days + 30),
        status=status,
    )
    Model.save(p)
    return SubscriptionPeriod.objects.get(pk=p.pk)


# ==============================================================================
# 1. Subscription Model Validation Tests
# ==============================================================================

class SubscriptionModelValidationTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()

    def test_create_valid_subscription_passes_validation(self):
        """A subscription with a valid sub_ ID and proper dates should save cleanly."""
        sub = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_valid12345",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=_now_plus(30),
        )
        sub.full_clean()   # Must not raise
        sub.save()
        self.assertEqual(Subscription.objects.count(), 1)

    def test_invalid_stripe_subscription_id_prefix_raises(self):
        """stripe_subscription_id must begin with 'sub_'."""
        sub = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="INVALID_ABC123",
            status=SubscriptionStatusChoice.ACTIVE,
        )
        with self.assertRaises(ValidationError) as ctx:
            sub.full_clean()
        self.assertIn("stripe_subscription_id", ctx.exception.message_dict)

    def test_second_active_subscription_for_same_user_raises(self):
        """Only one ACTIVE or TRIALING subscription is allowed per user."""
        # First active subscription saved cleanly
        first = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_first001",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=_now_plus(30),
        )
        first.full_clean()
        first.save()

        # Second ACTIVE subscription for same user must fail
        second = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_second002",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=_now_plus(30),
        )
        with self.assertRaises(ValidationError) as ctx:
            second.full_clean()
        self.assertIn("status", ctx.exception.message_dict)

    def test_canceled_subscription_does_not_block_new_active(self):
        """A historical CANCELED subscription must not block a new ACTIVE one."""
        _create_subscription_raw(
            self.user, self.plan,
            status=SubscriptionStatusChoice.CANCELED,
            stripe_id="sub_old_canceled",
        )
        # New ACTIVE subscription should succeed
        new_sub = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_new_active",
            status=SubscriptionStatusChoice.ACTIVE,
            current_period_start=timezone.now(),
            current_period_end=_now_plus(30),
        )
        new_sub.full_clean()  # Must not raise
        new_sub.save()
        self.assertEqual(
            Subscription.objects.filter(user=self.user).count(), 2
        )

    def test_period_end_before_start_raises(self):
        """current_period_end before current_period_start must be rejected."""
        now = timezone.now()
        sub = Subscription(
            user=self.user,
            plan=self.plan,
            amount=self.plan.amount, currency=self.plan.currency, billing_interval=self.plan.billing_interval, stripe_price_id=self.plan.stripe_price_id,
            stripe_subscription_id="sub_bad_dates",
            status=SubscriptionStatusChoice.INCOMPLETE,
            current_period_start=now,
            current_period_end=now - timedelta(days=1),  # Invalid: end before start
        )
        with self.assertRaises(ValidationError) as ctx:
            sub.full_clean()
        self.assertIn("current_period_end", ctx.exception.message_dict)


# ==============================================================================
# 2. SubscriptionPeriod Model Validation Tests
# ==============================================================================

class SubscriptionPeriodValidationTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()
        self.sub = _create_subscription_raw(self.user, self.plan)

    def test_valid_period_saves_cleanly(self):
        now = timezone.now()
        p = SubscriptionPeriod(
            subscription=self.sub,
            start_date=now,
            end_date=now + timedelta(days=30),
        )
        p.full_clean()
        p.save()
        self.assertEqual(SubscriptionPeriod.objects.count(), 1)

    def test_period_end_equal_to_start_raises(self):
        now = timezone.now()
        p = SubscriptionPeriod(
            subscription=self.sub,
            start_date=now,
            end_date=now,  # Must be strictly after
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("end_date", ctx.exception.message_dict)

    def test_period_end_before_start_raises(self):
        now = timezone.now()
        p = SubscriptionPeriod(
            subscription=self.sub,
            start_date=now,
            end_date=now - timedelta(hours=1),
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("end_date", ctx.exception.message_dict)


# ==============================================================================
# 3. Computed Property Tests
# ==============================================================================

class SubscriptionPropertiesTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()

    def _sub_with_status(self, status, cancel_at_period_end=False, stripe_suffix=""):
        return _create_subscription_raw(
            self.user, self.plan,
            status=status,
            stripe_id=f"sub_prop_{status}{stripe_suffix}",
            cancel_at_period_end=cancel_at_period_end,
        )

    def test_is_active_true_for_active_status(self):
        sub = self._sub_with_status(SubscriptionStatusChoice.ACTIVE, stripe_suffix="1")
        self.assertTrue(sub.is_active)

    def test_is_active_true_for_trialing_status(self):
        sub = self._sub_with_status(SubscriptionStatusChoice.TRIALING, stripe_suffix="2")
        self.assertTrue(sub.is_active)

    def test_is_active_false_for_past_due(self):
        sub = self._sub_with_status(SubscriptionStatusChoice.PAST_DUE, stripe_suffix="3")
        self.assertFalse(sub.is_active)

    def test_is_active_false_for_canceled(self):
        sub = self._sub_with_status(SubscriptionStatusChoice.CANCELED, stripe_suffix="4")
        self.assertFalse(sub.is_active)

    def test_is_past_due_true_only_for_past_due(self):
        sub_pd = self._sub_with_status(SubscriptionStatusChoice.PAST_DUE, stripe_suffix="5")
        sub_ac = self._sub_with_status(SubscriptionStatusChoice.ACTIVE, stripe_suffix="6")
        self.assertTrue(sub_pd.is_past_due)
        self.assertFalse(sub_ac.is_past_due)

    def test_is_pending_cancellation_true_when_active_and_cancel_flag(self):
        sub = self._sub_with_status(
            SubscriptionStatusChoice.ACTIVE,
            cancel_at_period_end=True,
            stripe_suffix="7",
        )
        self.assertTrue(sub.is_pending_cancellation)

    def test_is_pending_cancellation_false_without_cancel_flag(self):
        sub = self._sub_with_status(
            SubscriptionStatusChoice.ACTIVE,
            cancel_at_period_end=False,
            stripe_suffix="8",
        )
        self.assertFalse(sub.is_pending_cancellation)


# ==============================================================================
# 4. Manager Tests
# ==============================================================================

class SubscriptionManagerTest(TestCase):

    def setUp(self):
        self.u1 = _make_user("user1", "u1@test.com")
        self.u2 = _make_user("user2", "u2@test.com")
        self.pp = _make_plan()

    def test_active_queryset_returns_active_and_trialing_only(self):
        _create_subscription_raw(self.u1, self.pp, SubscriptionStatusChoice.ACTIVE,
                                 stripe_id="sub_m_active")
        _create_subscription_raw(self.u2, self.pp, SubscriptionStatusChoice.TRIALING,
                                 stripe_id="sub_m_trial")
        # Canceled should not appear
        _create_subscription_raw(self.u1, self.pp, SubscriptionStatusChoice.CANCELED,
                                 stripe_id="sub_m_canceled")

        active = Subscription.objects.active()
        self.assertEqual(active.count(), 2)
        statuses = set(active.values_list("status", flat=True))
        self.assertEqual(
            statuses,
            {SubscriptionStatusChoice.ACTIVE, SubscriptionStatusChoice.TRIALING},
        )

    def test_get_active_for_user_returns_none_when_no_subscription(self):
        result = Subscription.objects.get_active_for_user(self.u1)
        self.assertIsNone(result)

    def test_get_active_for_user_returns_active_subscription(self):
        sub = _create_subscription_raw(self.u1, self.pp, stripe_id="sub_for_user")
        result = Subscription.objects.get_active_for_user(self.u1)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, sub.pk)

    def test_get_active_for_user_ignores_canceled(self):
        _create_subscription_raw(
            self.u1, self.pp,
            status=SubscriptionStatusChoice.CANCELED,
            stripe_id="sub_canceled_user",
        )
        result = Subscription.objects.get_active_for_user(self.u1)
        self.assertIsNone(result)

    def test_period_manager_current_returns_active_period(self):
        sub = _create_subscription_raw(self.u1, self.pp, stripe_id="sub_period_test")
        period = _create_period_raw(sub, status=PeriodStatusChoice.ACTIVE)
        _create_period_raw(sub, status=PeriodStatusChoice.COMPLETED, offset_days=-35)

        current = SubscriptionPeriod.objects.current(sub)
        self.assertIsNotNone(current)
        self.assertEqual(current.pk, period.pk)
        self.assertEqual(current.status, PeriodStatusChoice.ACTIVE)


# ==============================================================================
# 5. Webhook Handler Tests
# ==============================================================================

class CheckoutSessionCompletedHandlerTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()
        # Create StripeCustomer mapping
        self.stripe_customer = StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_test_webhook",
        )

    def _make_payload(self, stripe_sub_id="sub_wh_001"):
        return {
            "data": {
                "object": {
                    "id": "cs_test_session001",
                    "customer": "cus_test_webhook",
                    "subscription": stripe_sub_id,
                    "metadata": {
                        "django_plan_id": str(self.plan.pk),
                    },
                }
            }
        }

    def _mock_stripe_sub(self, stripe_sub_id, status="active"):
        now = timezone.now()
        mock_sub = {
            "id": stripe_sub_id,
            "status": status,
            "current_period_start": int(now.timestamp()),
            "current_period_end": int((now + timedelta(days=30)).timestamp()),
            "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": self.plan.stripe_price_id}}]},
        }
        return mock_sub

    def test_creates_subscription_and_period_on_checkout_completed(self):
        """
        checkout.session.completed must create a local Subscription + SubscriptionPeriod.
        """
        stripe_sub_id = "sub_checkout_001"
        payload = self._make_payload(stripe_sub_id)
        mock_sub = self._mock_stripe_sub(stripe_sub_id)

        with patch("payments.stripe.handlers.events.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = mock_sub
            handle_checkout_session_completed(payload)

        # Verify Subscription created
        self.assertEqual(Subscription.objects.count(), 1)
        sub = Subscription.objects.first()
        self.assertEqual(sub.stripe_subscription_id, stripe_sub_id)
        self.assertEqual(sub.user, self.user)
        self.assertEqual(sub.plan, self.plan)
        self.assertEqual(sub.status, "active")

        # Verify SubscriptionPeriod created
        self.assertEqual(SubscriptionPeriod.objects.count(), 1)
        period = SubscriptionPeriod.objects.first()
        self.assertEqual(period.subscription, sub)
        self.assertEqual(period.status, PeriodStatusChoice.ACTIVE)

    def test_handler_is_idempotent_on_duplicate_delivery(self):
        """
        Running the handler twice for the same checkout session must not
        create duplicate Subscription or SubscriptionPeriod records.
        """
        stripe_sub_id = "sub_idempotent_001"
        payload = self._make_payload(stripe_sub_id)
        mock_sub = self._mock_stripe_sub(stripe_sub_id)

        with patch("payments.stripe.handlers.events.stripe") as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = mock_sub
            handle_checkout_session_completed(payload)
            handle_checkout_session_completed(payload)  # Replay

        self.assertEqual(Subscription.objects.count(), 1)
        self.assertEqual(SubscriptionPeriod.objects.count(), 1)


class SubscriptionUpdatedHandlerTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()
        self.subscription = _create_subscription_raw(
            self.user, self.plan, stripe_id="sub_update_test"
        )
        self.period = _create_period_raw(self.subscription, status=PeriodStatusChoice.ACTIVE)

    def _make_updated_payload(self, stripe_sub_id, new_status="active",
                              period_start=None, period_end=None,
                              cancel_at_period_end=False):
        now = timezone.now()
        ps = period_start or (now + timedelta(days=30))
        pe = period_end   or (now + timedelta(days=60))
        return {
            "data": {
                "object": {
                    "id": stripe_sub_id,
                    "status": new_status,
                    "current_period_start": int(ps.timestamp()),
                    "current_period_end":   int(pe.timestamp()),
                    "cancel_at_period_end": cancel_at_period_end,
                    "items": {
                        "data": [{"price": {"id": self.plan.stripe_price_id}}]
                    },
                }
            }
        }

    def test_subscription_status_synced_on_update(self):
        """Status change (e.g. past_due) must be reflected locally."""
        payload = self._make_updated_payload("sub_update_test", new_status="past_due")
        handle_customer_subscription_updated(payload)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatusChoice.PAST_DUE)

    def test_period_renewed_closes_old_and_opens_new(self):
        """When period dates advance, old period → COMPLETED and new one created."""
        payload = self._make_updated_payload("sub_update_test")
        handle_customer_subscription_updated(payload)

        # Old period must now be COMPLETED
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatusChoice.COMPLETED)

        # New ACTIVE period must have been created
        new_period = SubscriptionPeriod.objects.filter(
            subscription=self.subscription,
            status=PeriodStatusChoice.ACTIVE,
        ).first()
        self.assertIsNotNone(new_period)
        self.assertNotEqual(new_period.pk, self.period.pk)

    def test_cancel_at_period_end_flag_synced(self):
        """cancel_at_period_end flag from Stripe must be reflected locally."""
        payload = self._make_updated_payload(
            "sub_update_test", cancel_at_period_end=True,
            # Use same period to avoid renewal branch
            period_start=self.subscription.current_period_start,
            period_end=self.subscription.current_period_end,
        )
        handle_customer_subscription_updated(payload)

        self.subscription.refresh_from_db()
        self.assertTrue(self.subscription.cancel_at_period_end)

    def test_missing_subscription_handled_gracefully(self):
        """A subscription not in our DB should not raise; handler exits cleanly."""
        payload = self._make_updated_payload("sub_nonexistent_999")
        # Must not raise any exception
        handle_customer_subscription_updated(payload)


class SubscriptionDeletedHandlerTest(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.plan = _make_plan()
        self.subscription = _create_subscription_raw(
            self.user, self.plan, stripe_id="sub_del_test"
        )
        self.period = _create_period_raw(self.subscription, status=PeriodStatusChoice.ACTIVE)

    def test_subscription_marked_canceled_on_deletion_event(self):
        payload = {
            "data": {"object": {"id": "sub_del_test"}}
        }
        handle_customer_subscription_deleted(payload)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatusChoice.CANCELED)
        self.assertIsNotNone(self.subscription.canceled_at)

    def test_active_periods_closed_on_deletion(self):
        payload = {
            "data": {"object": {"id": "sub_del_test"}}
        }
        handle_customer_subscription_deleted(payload)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatusChoice.CANCELED)

    def test_missing_subscription_handled_gracefully(self):
        payload = {
            "data": {"object": {"id": "sub_totally_unknown"}}
        }
        # Must not raise
        handle_customer_subscription_deleted(payload)
