"""
Stripe Outbound Services.
Handles communication with Stripe API for catalog management, customers, and checkout.
"""

import logging
from typing import Optional
from django.conf import settings
import stripe
from payments.models import Plan
from payments.stripe.client import StripeClient


logger = logging.getLogger(__name__)


class StripeCatalogService:
    """
    Class-based service for synchronizing Django Plans and Plans
    with Stripe Products and recurring Prices.
    """

    @classmethod
    def create_or_get_product(cls, plan: Plan) -> str:
        """
        Creates or retrieves a Stripe Product corresponding to the Django Plan.
        
        Args:
            plan: The Plan model instance.
            
        Returns:
            The Stripe Product ID (e.g. 'prod_xxxx').
        """
        StripeClient.initialize()

        # Check existing Stripe products by metadata
        try:
            products = stripe.Product.search(
                query=f"metadata['django_plan_slug']:'{plan.slug}' AND active:'true'"
            )
            if products.data:
                product_id = products.data[0].id
                logger.info("Found existing Stripe Product '%s' for Plan '%s'", product_id, plan.slug)
                return product_id
        except Exception as search_err:
            logger.debug("Stripe Product search not available or failed: %s. Creating new product.", search_err)

        # Create new product on Stripe
        try:
            product = stripe.Product.create(
                name=plan.name,
                description=plan.description or f"{plan.name} Subscription Plan",
                metadata={
                    "django_plan_id": str(plan.id),
                    "django_plan_slug": plan.slug,
                },
            )
            logger.info("Created new Stripe Product '%s' for Plan '%s'", product.id, plan.slug)
            return product.id
        except stripe.StripeError as e:
            logger.error("Failed to create Stripe Product for Plan '%s': %s", plan.slug, str(e))
            raise

    @classmethod
    def create_recurring_price(cls, plan: Plan, stripe_product_id: str) -> str:
        """
        Creates a recurring Price in Stripe for a specific Plan.
        
        Args:
            plan: The Plan instance.
            stripe_product_id: The Stripe Product ID to attach the price to.
            
        Returns:
            The Stripe Price ID (e.g. 'price_xxxx').
        """
        StripeClient.initialize()

        try:
            price = stripe.Price.create(
                product=stripe_product_id,
                unit_amount=plan.get_amount_in_cents(),
                currency=plan.currency,
                recurring={
                    "interval": plan.billing_interval,
                },
                metadata={
                    "django_plan_id": str(plan.id),
                    "django_plan_slug": plan.slug,
                    "interval": plan.billing_interval,
                },
            )
            logger.info("Created Stripe Price '%s' for Plan #%s", price.id, plan.id)
            return price.id
        except stripe.StripeError as e:
            logger.error("Failed to create Stripe Price for Plan #%s: %s", plan.id, str(e))
            raise

    @classmethod
    def sync_plan(cls, plan: Plan) -> Plan:
        """
        High-level synchronization orchestrator:
        1. Ensures a Stripe Product exists for the plan.
        2. Creates a recurring Price in Stripe.
        3. Updates and saves plan.stripe_price_id.
        """
        product_id = cls.create_or_get_product(plan)
        stripe_price_id = cls.create_recurring_price(plan, product_id)

        plan.stripe_price_id = stripe_price_id
        plan.save(update_fields=["stripe_price_id", "updated_at"])
        return plan


class StripeCustomerService:
    """
    Class-based service managing Stripe Customer creation, retrieval,
    and mapping to Django users.
    """

    @classmethod
    def get_or_create_customer(cls, user) -> "StripeCustomer":
        """
        Retrieves the local StripeCustomer if it already exists, or creates
        a new Customer record in Stripe and persists the mapping locally.

        Args:
            user: Django User instance.

        Returns:
            StripeCustomer model instance.
        """
        from payments.models import StripeCustomer


        # 1. Return existing local Stripe customer if available
        customer = StripeCustomer.objects.filter(user=user).first()
        if customer:
            logger.info(
                "Reusing existing StripeCustomer '%s' for user '%s'",
                customer.stripe_customer_id,
                user.username,
            )
            return customer

        # 2. Initialize Stripe client and create customer in Stripe
        StripeClient.initialize()
        try:
            name = user.get_full_name() or user.username
            stripe_res = stripe.Customer.create(
                email=user.email or None,
                name=name,
                metadata={
                    "django_user_id": str(user.id),
                    "username": user.username,
                },
            )
            logger.info(
                "Created new Stripe Customer '%s' for user '%s'",
                stripe_res.id,
                user.username,
            )

            # 3. Save mapping locally
            customer = StripeCustomer.objects.create(
                user=user,
                stripe_customer_id=stripe_res.id,
            )
            return customer

        except stripe.StripeError as e:
            logger.error(
                "Failed to create Stripe Customer for user '%s': %s",
                user.username,
                str(e),
            )
            raise

    @classmethod
    def retrieve_stripe_customer(cls, stripe_customer_id: str) -> stripe.Customer:
        """
        Retrieves the customer object directly from Stripe.
        """
        StripeClient.initialize()
        return stripe.Customer.retrieve(stripe_customer_id)


class StripeCheckoutService:
    """
    Class-based service for initiating Stripe Checkout Sessions
    in subscription mode.
    """

    @classmethod
    def create_subscription_checkout_session(
        cls,
        user,
        plan: Plan,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
    ) -> stripe.checkout.Session:
        """
        Creates a Stripe Checkout Session in subscription mode.

        Args:
            user: Django User instance.
            plan: The Plan to subscribe to.
            success_url: Optional custom redirect URL upon completion.
            cancel_url: Optional custom redirect URL upon cancellation.

        Returns:
            The created stripe.checkout.Session instance.
        """
        StripeClient.initialize()

        # 1. Ensure the plan price is synced with Stripe
        if not plan.is_synced_with_stripe:
            logger.info("Plan #%s not synced with Stripe. Auto-syncing now...", plan.id)
            plan = StripeCatalogService.sync_plan(plan)

        # 2. Retrieve or create Stripe customer for this user
        customer = StripeCustomerService.get_or_create_customer(user)

        # 3. Resolve redirect URLs with fallbacks
        resolved_success_url = success_url or settings.FRONTEND_SUCCESS_URL
        resolved_cancel_url = cancel_url or settings.FRONTEND_CANCEL_URL

        from payments.stripe.constants import CHECKOUT_MODE_SUBSCRIPTION

        # 4. Create Stripe Checkout Session
        try:
            session = stripe.checkout.Session.create(
                mode=CHECKOUT_MODE_SUBSCRIPTION,
                customer=customer.stripe_customer_id,
                line_items=[
                    {
                        "price": plan.stripe_price_id,
                        "quantity": 1,
                    }
                ],
                success_url=resolved_success_url,
                cancel_url=resolved_cancel_url,
                subscription_data={
                    "metadata": {
                        "django_user_id": str(user.id),
                        "django_plan_id": str(plan.id),
                        "django_plan_slug": plan.slug,
                    }
                },
                metadata={
                    "django_user_id": str(user.id),
                    "django_plan_id": str(plan.id),
                    "django_plan_slug": plan.slug,
                },
            )
            logger.info(
                "Created Stripe Checkout Session '%s' for user '%s' on price #%s",
                session.id,
                user.username,
                plan.id,
            )
            return session

        except stripe.StripeError as e:
            logger.error(
                "Failed to create Stripe Checkout Session for user '%s': %s",
                user.username,
                str(e),
            )
            raise


class StripeWebhookVerificationService:
    """
    Class-based service for verifying cryptographic signatures
    and parsing Stripe webhook event payloads.
    """

    @classmethod
    def verify_and_construct_event(cls, payload: bytes, sig_header: str) -> dict:
        """
        Cryptographically verifies the Stripe webhook signature header against the payload.

        Args:
            payload: Raw request body as bytes.
            sig_header: Value of the Stripe signature header (HTTP_STRIPE_SIGNATURE).

        Returns:
            The parsed Stripe event dictionary.

        Raises:
            ValueError: If webhook secret or signature is missing.
            stripe.SignatureVerificationError: If cryptographic signature verification fails.
        """
        from payments.stripe.constants import DEFAULT_WEBHOOK_TOLERANCE_SECONDS

        webhook_secret = StripeClient.get_webhook_secret()
        if not webhook_secret:
            logger.error("STRIPE_WEBHOOK_SECRET is not configured.")
            raise ValueError("Stripe webhook signing secret is not configured.")

        if not sig_header:
            logger.warning("Missing Stripe signature header in webhook request.")
            raise ValueError("Stripe signature header is missing.")

        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret,
            tolerance=DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
        )
        return event


# ==============================================================================
# Subscription Lifecycle Service
# ==============================================================================

class StripeSubscriptionService:
    """
    Outbound Stripe API calls for subscription lifecycle management.

    Design note: These methods issue requests to Stripe and return immediately.
    The actual local DB state transition (status, cancel_at_period_end, etc.)
    happens asynchronously when Stripe delivers the corresponding webhook event
    and our dispatcher processes it.
    """

    @classmethod
    def cancel_subscription(
        cls,
        subscription: "Subscription",  # noqa: F821 – forward ref
        at_period_end: bool = True,
    ) -> dict:
        """
        Cancel a Stripe subscription.

        Args:
            subscription: The local Subscription model instance.
            at_period_end: If True, schedules cancellation at current_period_end
                           (customer retains access until then).
                           If False, cancels immediately.

        Returns:
            The raw Stripe Subscription dict returned by the API.

        Raises:
            stripe.StripeError: On Stripe API failure.
        """
        StripeClient.initialize()
        if at_period_end:
            result = stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=True,
            )
            logger.info(
                "Scheduled at-period-end cancellation for subscription '%s'",
                subscription.stripe_subscription_id,
            )
        else:
            result = stripe.Subscription.cancel(
                subscription.stripe_subscription_id,
            )
            logger.info(
                "Immediately canceled subscription '%s'",
                subscription.stripe_subscription_id,
            )
        return result


    @classmethod
    def reactivate_subscription(cls, subscription: "Subscription") -> dict:  # noqa: F821
        """
        Undo a pending at-period-end cancellation (sets cancel_at_period_end=False).
        Only valid while the subscription is still within its current period.

        Args:
            subscription: The local Subscription model instance.

        Returns:
            The raw Stripe Subscription dict returned by the API.

        Raises:
            stripe.StripeError: On Stripe API failure.
        """
        StripeClient.initialize()
        result = stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            cancel_at_period_end=False,
        )
        logger.info(
            "Reactivated subscription '%s' (cancel_at_period_end → False)",
            subscription.stripe_subscription_id,
        )
        return result


class StripePortalService:
    """Creates Stripe-hosted Customer Billing Portal sessions."""

    @classmethod
    def create_portal_session(cls, user, return_url: str | None = None) -> str:
        from django.conf import settings
        from payments.models import StripeCustomer

        try:
            stripe_customer = StripeCustomer.objects.get(user=user)
        except StripeCustomer.DoesNotExist as exc:
            raise ValueError(
                f"No StripeCustomer record found for user {user.username!r}. "
                "The user must initiate a checkout session first."
            ) from exc

        StripeClient.initialize()
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer.stripe_customer_id,
            return_url=return_url
            or getattr(settings, "STRIPE_PORTAL_RETURN_URL", "http://localhost:8000/"),
        )
        return session.url
