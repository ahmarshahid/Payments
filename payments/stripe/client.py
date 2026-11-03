"""
Stripe SDK Client Initializer and Factory.
Provides a centralized, configured client for Stripe API calls.
"""

import logging
from typing import Optional
from django.conf import settings
import stripe

logger = logging.getLogger(__name__)


class StripeClient:
    """
    Singleton wrapper for Stripe SDK configuration and client access.
    """

    _initialized: bool = False

    @classmethod
    def initialize(cls) -> None:
        """
        Configure the Stripe library with the configured secret key.
        """
        if not settings.STRIPE_SECRET_KEY:
            logger.warning(
                "STRIPE_SECRET_KEY is not configured in settings. "
                "Outbound Stripe API requests will fail."
            )
            return

        stripe.api_key = settings.STRIPE_SECRET_KEY
        cls._initialized = True

    @classmethod
    def get_api_key(cls) -> str:
        """
        Retrieve the current Stripe secret key.
        """
        if not cls._initialized:
            cls.initialize()
        return settings.STRIPE_SECRET_KEY

    @classmethod
    def get_webhook_secret(cls) -> str:
        """
        Retrieve the Stripe webhook signing secret.
        """
        return settings.STRIPE_WEBHOOK_SECRET

    @classmethod
    def get_publishable_key(cls) -> str:
        """
        Retrieve the Stripe publishable key.
        """
        return settings.STRIPE_PUBLISHABLE_KEY


# Initialize upon module import
StripeClient.initialize()
