"""
URL Configuration for Payments and Subscription Management.
"""

from django.urls import path
from payments.stripe.views import (
    CreateCheckoutSessionView,
    CheckoutSuccessView,
    CheckoutCancelView,
    StripeWebhookView,
)
from payments.views import (
    PlanListAPIView,
    PlanDetailAPIView,
    
    CurrentSubscriptionView,
    CancelSubscriptionView,
    ReactivateSubscriptionView,
    UsageSummaryView,
    FeatureAccessCheckView,
    BillingHistoryView,
    CreateBillingPortalSessionView,
)

app_name = "payments"

urlpatterns = [
    # ── Catalog ────────────────────────────────────────────────────────────────
    path("api/plans/",              PlanListAPIView.as_view(),   name="plan-list"),
    path("api/plans/<slug:slug>/",  PlanDetailAPIView.as_view(), name="plan-detail"),

    # ── Stripe Checkout ────────────────────────────────────────────────────────
    path("api/checkout/",           CreateCheckoutSessionView.as_view(), name="create-checkout-session"),
    path("success/",                CheckoutSuccessView.as_view(),       name="checkout-success"),
    path("cancel/",                 CheckoutCancelView.as_view(),        name="checkout-cancel"),

    # ── Stripe Webhooks ────────────────────────────────────────────────────────
    path("webhooks/stripe/",        StripeWebhookView.as_view(),         name="stripe-webhook"),

    # ── Phase 7: Subscription Lifecycle ───────────────────────────────────────
    path("api/subscriptions/current/",    CurrentSubscriptionView.as_view(),    name="subscription-current"),
    path("api/subscriptions/cancel/",     CancelSubscriptionView.as_view(),     name="subscription-cancel"),
    path("api/subscriptions/reactivate/", ReactivateSubscriptionView.as_view(), name="subscription-reactivate"),

    # ── Phase 8: Usage & Entitlement ──────────────────────────────────────────
    path("api/usage/", UsageSummaryView.as_view(), name="usage-summary"),
    path("api/usage/check/<slug:feature_slug>/", FeatureAccessCheckView.as_view(), name="usage-check"),

    # ── Phase 9: Billing history & portal ─────────────────────────────────────
    path("api/billing/history/", BillingHistoryView.as_view(), name="billing-history"),
    path("api/billing/portal/", CreateBillingPortalSessionView.as_view(), name="billing-portal"),
]
