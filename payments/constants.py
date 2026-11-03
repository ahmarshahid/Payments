"""
Centralized Application Constants for Payments and Subscriptions.
"""

from typing import Final

# Default Currency
DEFAULT_CURRENCY: Final[str] = "usd"

# Default Billing Interval
DEFAULT_BILLING_INTERVAL: Final[str] = "month"

# Pagination defaults
DEFAULT_PAGE_SIZE: Final[int] = 20
MAX_PAGE_SIZE: Final[int] = 100

# Feature Limits
UNLIMITED_FEATURE_LIMIT: Final[int] = -1

# Slug configuration
MAX_SLUG_LENGTH: Final[int] = 100

# Subscription constants
MAX_ACTIVE_SUBSCRIPTIONS_PER_USER: Final[int] = 1

# Stripe object ID prefixes
STRIPE_SUBSCRIPTION_ID_PREFIX: Final[str] = "sub_"
STRIPE_INVOICE_ID_PREFIX: Final[str] = "in_"
STRIPE_PAYMENT_INTENT_ID_PREFIX: Final[str] = "pi_"

# Feature usage
FEATURE_QUOTA_UNIT_DEFAULT: Final[str] = "units"

# Entitlement error codes
QUOTA_EXCEEDED_CODE: Final[str] = "quota_exceeded"
NO_ACTIVE_SUBSCRIPTION_CODE: Final[str] = "no_active_subscription"
FEATURE_DISABLED_CODE: Final[str] = "feature_disabled"
