"""
Webhook Event Handlers and Dispatcher Package.
Routes incoming Stripe events to dedicated handler functions via a Dictionary Dispatcher.
"""

from payments.stripe.handlers.dispatcher import StripeEventDispatcher

__all__ = ["StripeEventDispatcher"]
