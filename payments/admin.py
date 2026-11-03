from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from payments.models import (
    Feature, Plan, PlanFeature, PlanPrice,
    StripeCustomer, StripeWebhookEvent,

    Subscription, SubscriptionPeriod, FeatureUsage, StripePayment,
)
from payments.choices import SubscriptionStatusChoice
from payments.stripe.services import StripeCatalogService, StripeSubscriptionService

import stripe as stripe_sdk
from payments.stripe.client import StripeClient



class PlanFeatureInline(admin.TabularInline):
    """
    Inline editor for PlanFeature configurations directly within the Plan admin page.
    """
    model = PlanFeature
    extra = 1
    fields = ["feature", "is_enabled", "limit", "unit"]
    autocomplete_fields = ["feature"]
    show_change_link = True


class PlanPriceInline(admin.TabularInline):
    """
    Inline editor for PlanPrice options directly within the Plan admin page.
    """
    model = PlanPrice
    extra = 1
    fields = ["amount", "currency", "billing_interval", "stripe_price_id", "is_active"]
    readonly_fields = ["stripe_price_id"]
    show_change_link = True


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    """
    Admin management for application capabilities (Feature).
    """
    list_display = ["name", "slug", "type", "is_active", "created_at"]
    list_filter = ["type", "is_active"]
    search_fields = ["name", "slug", "description"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["-name"]

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description", "type", "is_active")
        }),
        (_("Timestamps"), {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    """
    Admin management for subscription tiers (Plan) with inline feature configuration.
    """
    list_display = ["name", "slug", "is_active", "price_count", "feature_count", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug", "description"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]
    inlines = [PlanPriceInline, PlanFeatureInline]
    ordering = ["name"]

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description", "is_active")
        }),
        (_("Timestamps"), {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description=_("Configured Prices"))
    def price_count(self, obj: Plan) -> int:
        return obj.prices.count()

    @admin.display(description=_("Configured Features"))
    def feature_count(self, obj: Plan) -> int:
        return obj.plan_features.count()


@admin.register(PlanFeature)
class PlanFeatureAdmin(admin.ModelAdmin):
    """
    Direct administration of Plan-Feature configurations.
    """
    list_display = ["plan", "feature", "get_feature_type", "is_enabled", "limit", "unit"]
    list_filter = ["plan", "feature__type", "is_enabled"]
    search_fields = ["plan__name", "feature__name"]
    autocomplete_fields = ["plan", "feature"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description=_("Feature Type"), ordering="feature__type")
    def get_feature_type(self, obj: PlanFeature) -> str:
        return obj.feature.get_type_display()


@admin.register(PlanPrice)
class PlanPriceAdmin(admin.ModelAdmin):
    """
    Direct administration of Plan Prices with Stripe synchronization.
    """
    list_display = [
        "plan",
        "amount",
        "currency",
        "billing_interval",
        "stripe_price_id",
        "is_synced",
        "is_active",
        "created_at",
    ]
    list_filter = ["billing_interval", "currency", "is_active", "plan"]
    search_fields = ["plan__name", "stripe_price_id"]
    autocomplete_fields = ["plan"]
    readonly_fields = ["created_at", "updated_at"]
    actions = ["sync_to_stripe"]

    @admin.display(boolean=True, description=_("Synced with Stripe"))
    def is_synced(self, obj: PlanPrice) -> bool:
        return obj.is_synced_with_stripe

    @admin.action(description=_("Synchronize selected prices to Stripe Sandbox"))
    def sync_to_stripe(self, request, queryset):
        success_count = 0
        for price in queryset:
            try:
                StripeCatalogService.sync_plan_price(price)
                success_count += 1
            except Exception as e:
                self.message_user(
                    request,
                    f"Failed to sync {price}: {str(e)}",
                    level=messages.ERROR,
                )
        if success_count:
            self.message_user(
                request,
                f"Successfully synced {success_count} price(s) to Stripe.",
                level=messages.SUCCESS,
            )


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):
    """
    Admin management for StripeCustomer mappings.
    """
    list_display = ["user", "get_user_email", "stripe_customer_id", "created_at"]
    search_fields = ["user__username", "user__email", "stripe_customer_id"]
    autocomplete_fields = ["user"]
    readonly_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    @admin.display(description=_("Email"), ordering="user__email")
    def get_user_email(self, obj: StripeCustomer) -> str:
        return obj.user.email


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    """
    Audit log of incoming Stripe webhook events.
    """
    list_display = ["stripe_event_id", "event_type", "status", "processed_at", "created_at"]
    list_filter = ["status", "event_type", "created_at"]
    search_fields = ["stripe_event_id", "event_type", "error_message"]
    readonly_fields = [
        "stripe_event_id",
        "event_type",
        "status",
        "payload",
        "error_message",
        "processed_at",
        "created_at",
        "updated_at",
    ]
    ordering = ["-created_at"]


# ==============================================================================
# : Subscription Admin
# ==============================================================================

class SubscriptionPeriodInline(admin.TabularInline):
    """
    Displays all billing periods for a subscription directly on the Subscription page.
    Read-only: periods are created exclusively by webhook handlers.
    """
    model        = SubscriptionPeriod
    extra        = 0
    can_delete   = False
    show_change_link = True
    readonly_fields  = ["start_date", "end_date", "status", "stripe_invoice_id", "created_at"]
    fields           = ["start_date", "end_date", "status", "stripe_invoice_id", "created_at"]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    """
    Admin for Subscription model.
    All fields are read-only — Stripe is the source of truth.
    Changes are made via Stripe dashboard or outbound API calls (cancel, reactivate).
    """
    list_display   = [
        "user", "plan_price", "status",
        "current_period_end", "cancel_at_period_end", "created_at",
    ]
    list_filter    = ["status", "cancel_at_period_end", "plan_price__billing_interval"]
    search_fields  = ["user__username", "user__email", "stripe_subscription_id"]
    readonly_fields = [
        "user", "plan_price", "stripe_subscription_id",
        "status", "started_at",
        "current_period_start", "current_period_end",
        "cancel_at_period_end", "canceled_at",
        "created_at", "updated_at",
    ]
    inlines = [SubscriptionPeriodInline]
    actions = ["action_cancel_at_period_end", "action_sync_status_from_stripe"]

    @admin.action(description="Cancel selected subscriptions at period end (Stripe)")
    def action_cancel_at_period_end(self, request, queryset):
        """Schedules cancellation for all selected active subscriptions."""
        active_subs = queryset.filter(
            status__in=[
                SubscriptionStatusChoice.ACTIVE,
                SubscriptionStatusChoice.TRIALING,
            ]
        )
        count = 0
        errors = 0
        for sub in active_subs:
            try:
                StripeSubscriptionService.cancel_subscription(sub, at_period_end=True)
                count += 1
            except Exception as exc:
                errors += 1
                self.message_user(
                    request,
                    f"Error canceling {sub.stripe_subscription_id}: {exc}",
                    messages.ERROR,
                )
        if count:
            self.message_user(
                request,
                f"Scheduled {count} subscription(s) for cancellation at period end.",
                messages.SUCCESS,
            )
        if errors:
            self.message_user(
                request,
                f"{errors} subscription(s) could not be canceled. See errors above.",
                messages.WARNING,
            )

    @admin.action(description="Sync status from Stripe (live API call)")
    def action_sync_status_from_stripe(self, request, queryset):
        """Pulls the latest status from Stripe and updates local records."""
        StripeClient.initialize()
        synced = 0
        for sub in queryset:
            try:
                stripe_sub = stripe_sdk.Subscription.retrieve(sub.stripe_subscription_id)
                sub.status = stripe_sub["status"]
                sub.cancel_at_period_end = stripe_sub.get("cancel_at_period_end", False)
                sub.save(update_fields=["status", "cancel_at_period_end", "updated_at"])
                synced += 1
            except Exception as exc:
                self.message_user(
                    request,
                    f"Error syncing {sub.stripe_subscription_id}: {exc}",
                    messages.ERROR,
                )
        if synced:
            self.message_user(
                request,
                f"Synced {synced} subscription(s) from Stripe.",
                messages.SUCCESS,
            )


@admin.register(FeatureUsage)
class FeatureUsageAdmin(admin.ModelAdmin):
    list_display = [
        "subscription",
        "feature",
        "used_amount",
        "period_start",
        "period_end",
    ]
    list_filter = ["feature__type", "period_start"]
    search_fields = ["subscription__user__username", "feature__name"]
    readonly_fields = [
        "subscription",
        "feature",
        "period_start",
        "period_end",
        "used_amount",
        "created_at",
    ]


@admin.register(StripePayment)
class StripePaymentAdmin(admin.ModelAdmin):
    list_display = [
        "subscription",
        "amount",
        "currency",
        "status",
        "paid_at",
        "stripe_invoice_id",
    ]
    list_filter = ["status", "currency"]
    search_fields = [
        "subscription__user__username",
        "stripe_invoice_id",
        "stripe_payment_intent_id",
    ]
    readonly_fields = [
        "subscription",
        "period",
        "stripe_payment_intent_id",
        "stripe_invoice_id",
        "amount",
        "currency",
        "status",
        "paid_at",
        "created_at",
        "updated_at",
    ]
    ordering = ["-paid_at"]
