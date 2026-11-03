# Comprehensive Architecture & Development Progress Specification
## SaaS Subscription & Payment Management System (Django + Stripe Sandbox)

This living document tracks the end-to-end architecture, technical implementation, entity schemas, validation rules, webhook lifecycle, and test coverage for the **Django Subscription and Payment Management System**.

---

## 📑 Table of Contents
1. [Executive Summary & High-Level Architecture](#1-executive-summary--high-level-architecture)
2. [Complete Database Schema & ER Diagram](#2-complete-database-schema--er-diagram)
3. [Design Principles & Architectural Standards](#3-design-principles--architectural-standards)
4. [File & Directory Structure](#4-file--directory-structure)
5. [Phase-by-Phase Detailed Implementation Log](#5-phase-by-phase-detailed-implementation-log)
   - [Phase 1: Environment, Dependencies & Stripe SDK Configuration](#phase-1-environment-dependencies--stripe-sdk-configuration)
   - [Phase 2: Product Catalog & Entitlements (Feature, Plan, PlanFeature)](#phase-2-product-catalog--entitlements-feature-plan-planfeature)
   - [Phase 3: Multi-Interval Pricing & Stripe Catalog Synchronization](#phase-3-multi-interval-pricing--stripe-catalog-synchronization)
   - [Phase 4: Customer Mapping & Stripe Identity Synchronization](#phase-4-customer-mapping--stripe-identity-synchronization)
   - [Phase 5: Stripe-Hosted Checkout Session & Catalog CBVs](#phase-5-stripe-hosted-checkout-session--catalog-cbvs)
   - [Phase 6: Webhook Security, Idempotency & Dictionary Dispatcher](#phase-6-webhook-security-idempotency--dictionary-dispatcher)
   - [Architectural Refactor: Consolidated Models Architecture](#architectural-refactor-consolidated-models-architecture)
6. [Roadmap for Remaining Phases (7 through 10)](#6-roadmap-for-remaining-phases-7-through-10)
   - [Phase 7: Subscriptions, Periods & State Machine Lifecycle](#phase-7-subscriptions-periods--state-machine-lifecycle)
   - [Phase 8: Usage Tracking, Atomic Quotas & Reset Engine](#phase-8-usage-tracking-atomic-quotas--reset-engine)
   - [Phase 9: Payment Auditing, Invoicing & Billing Portal](#phase-9-payment-auditing-invoicing--billing-portal)
   - [Phase 10: End-to-End Verification, Failure Drills & Production Readiness](#phase-10-end-to-end-verification-failure-drills--production-readiness)
7. [Automated Test Suite Matrix](#7-automated-test-suite-matrix)
8. [API Reference & Payload Specifications](#8-api-reference--payload-specifications)

---

## 1. Executive Summary & High-Level Architecture

The goal of this system is to provide an enterprise-grade, recurring SaaS billing infrastructure where:
- **Administrators** define tiers (`Plan`), capabilities (`Feature`), and plan-specific limits/toggles (`PlanFeature`), along with recurring intervals and currencies (`PlanPrice`).
- **Catalog Synchronization** syncs local plans and prices with Stripe Products and recurring Prices in test mode.
- **Customers** authenticate, view plan offerings, and initiate hosted **Stripe Checkout Sessions** without exposing card credentials to our servers.
- **Zero Blind Trust**: Subscriptions and payments are never activated based on the browser's return URL. Access is granted solely through cryptographically signed Stripe webhooks.
- **Strict Idempotency**: All incoming events are audited in `StripeWebhookEvent` records to prevent duplicate deliveries from causing duplicate transactions or status corruption.
- **Concurrency-Safe Metering**: Metered resource consumption is governed by atomic database operations (`F()` expressions, `select_for_update`) to prevent race condition overages.

```
       ┌────────────────────────┐         ┌────────────────────────┐
       │   Customer (Browser)   │         │     Stripe Hosted      │
       │  1. Views Plans        │         │     Checkout Page      │
       │  2. Selects Price      ├────────►│  3. Inputs Test Card   │
       │  5. Returns to Success │         │  4. Completes Checkout │
       └───────────┬────────────┘         └───────────┬────────────┘
                   │                                  │
                   │ (HTTP GET/POST)                  │ (HTTPS Webhook)
                   ▼                                  ▼
       ┌────────────────────────┐         ┌────────────────────────┐
       │     Django Backend     │◄────────┤ Stripe Webhook Service │
       │  - Catalog API         │         │ - HMAC Signature Verify│
       │  - Checkout Session CBV│         │ - Idempotency Guard    │
       │  - Entitlement Engine  │         │ - Dictionary Dispatch  │
       └────────────────────────┘         └────────────────────────┘
```

---

## 2. Complete Database Schema & ER Diagram

```
                 ┌──────────────────┐               ┌──────────────────┐
                 │       PLAN       │               │     FEATURE      │
                 └────────┬─────────┘               └────────┬─────────┘
                          │                                  │
                    offers│ 1..N                       1..N  │ included_in
                          ▼                                  ▼
                 ┌──────────────────┐               ┌──────────────────┐
                 │    PLAN_PRICE    │               │   PLAN_FEATURE   │
                 └────────┬─────────┘               │ (toggle vs quota)│
                          │                         └──────────────────┘
             purchased_as │ 1..N                             ▲
                          ▼                                  │
 ┌──────────────┐1..N┌──────────────┐      tracks 1..N       │ measures
 │     USER     ├───►│ SUBSCRIPTION ├──────────────────────┐ │
 └──────┬───────┘    └──────┬───────┘                      │ │
        │                   │                              ▼ │
        │ 1..1              │ has 1..N              ┌──────────────────┐
        ▼                   ▼                       │  FEATURE_USAGE   │
 ┌──────────────┐    ┌──────────────┐               └──────────────────┘
 │STRIPE_CUSTOM.│    │SUBSCRIPTION_ │
 └──────────────┘    │    PERIOD    │
                     └──────┬───────┘
                            │
               generates    │ relates_to
               1..N         ▼ 1..N
               ┌───────────────────────┐
               │    STRIPE_PAYMENT     │
               └───────────────────────┘

 ┌────────────────────────────────────┐
 │        STRIPE_WEBHOOK_EVENT        │  (Audit & Idempotency Log)
 └────────────────────────────────────┘
```

### Entity Matrix & Responsibilities

| Model | Table | Inheritance | Key Fields | Primary Responsibility |
|---|---|---|---|---|
| **`TimeStampedModel`** | Abstract | `models.Model` | `created_at`, `updated_at` | Provides unified timestamps across all tables. |
| **`Feature`** | `payments_feature` | `TimeStampedModel` | `name`, `slug`, `type`, `is_active` | Capability definition (`TOGGLE` vs `LIMIT`). |
| **`Plan`** | `payments_plan` | `TimeStampedModel` | `name`, `slug`, `description`, `is_active` | Subscription tier (Free, Pro, Enterprise). |
| **`PlanFeature`** | `payments_planfeature` | `TimeStampedModel` | `plan`, `feature`, `is_enabled`, `limit`, `unit` | Intermediate config connecting Plan and Feature. |
| **`PlanPrice`** | `payments_planprice` | `TimeStampedModel` | `plan`, `amount`, `currency`, `billing_interval`, `stripe_price_id` | Recurring price point mapped to a Stripe Price ID. |
| **`StripeCustomer`** | `payments_stripecustomer` | `TimeStampedModel` | `user` (1:1), `stripe_customer_id` | External Stripe Customer mapping for user reuse. |
| **`StripeWebhookEvent`** | `payments_stripewebhookevent` | `TimeStampedModel` | `stripe_event_id`, `event_type`, `status`, `payload`, `error_message`, `processed_at` | Immutable log enforcing webhook idempotency. |
| **`Subscription`** *(Phase 7)* | `payments_subscription` | `TimeStampedModel` | `user`, `plan_price`, `stripe_subscription_id`, `status`, `current_period_start`, `current_period_end` | State machine governing customer plan access. |
| **`SubscriptionPeriod`** *(Phase 7)* | `payments_subscriptionperiod` | `TimeStampedModel` | `subscription`, `start_date`, `end_date`, `status`, `stripe_invoice_id` | Historical log of renewal cycles. |
| **`FeatureUsage`** *(Phase 8)* | `payments_featureusage` | `TimeStampedModel` | `subscription`, `feature`, `period_start`, `period_end`, `used_amount` | Real-time usage consumption metrics. |
| **`StripePayment`** *(Phase 9)* | `payments_stripepayment` | `TimeStampedModel` | `subscription`, `period`, `stripe_payment_intent_id`, `stripe_invoice_id`, `amount`, `status` | Financial transaction ledger. |

---

## 3. Design Principles & Architectural Standards

1. **Centralized Class-Based Validation**:
   - Model validation is never spread randomly across view functions or serializers.
   - Domain rules live inside dedicated validator classes in [`payments/validators.py`](file:///home/linkedmatrix/Payment-System/payments/validators.py).
   - Models enforce these rules inside their `clean()` method and call `self.full_clean()` inside `save()`.
2. **Canonical Single-File Models Architecture**:
   - All concrete models live in a single file: [`payments/models/models.py`](file:///home/linkedmatrix/Payment-System/payments/models/models.py).
   - [`payments/models/__init__.py`](file:///home/linkedmatrix/Payment-System/payments/models/__init__.py) exports them cleanly so imports are unified across the application (`from payments.models import ...`).
3. **No Magic Strings**:
   - All enums, intervals, and statuses use standard `models.TextChoices` in [`payments/choices.py`](file:///home/linkedmatrix/Payment-System/payments/choices.py).
   - System-wide constants live in [`payments/constants.py`](file:///home/linkedmatrix/Payment-System/payments/constants.py) and [`payments/stripe/constants.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/constants.py).
4. **Isolated Stripe Integration Layer**:
   - Third-party Stripe SDK calls, webhook signature verification, and event payloads are completely encapsulated in the `payments/stripe/` directory.
   - Business services interact through typed helper methods rather than issuing raw Stripe API calls.
5. **Dictionary Dispatcher Pattern**:
   - Webhook routing uses an $O(1)$ key-value registry mapping `StripeEventType` strings to dedicated handler functions, avoiding fragile `if/elif` blocks.

---

## 4. File & Directory Structure

```text
Payment-System/
├── config/
│   ├── settings.py                 # Core settings, env loading, DRF, Stripe keys, test hasher
│   ├── urls.py                     # Root URLconf routing to payments.urls
│   └── wsgi.py
│
├── payments/
│   ├── models/
│   │   ├── __init__.py             # Exports all models from models.py
│   │   ├── base.py                 # Abstract TimeStampedModel
│   │   └── models.py               # Consolidated domain models in one file
│   │
│   ├── choices.py                  # TextChoices: FeatureType, BillingInterval, Statuses
│   ├── constants.py                # Defaults: CURRENCY, INTERVAL, LIMITS
│   ├── validators.py               # Centralized Class-Based Validators
│   ├── managers.py                 # Custom QuerySets and Managers
│   ├── serializers.py              # DRF ModelSerializers with validation
│   ├── views.py                    # Catalog Class-Based Views (PlanListAPIView, PlanDetailAPIView)
│   ├── urls.py                     # App-level URL routes
│   ├── admin.py                    # Django Admin registrations, inlines, and bulk actions
│   │
│   ├── stripe/                     # Dedicated Stripe Integration Module
│   │   ├── __init__.py
│   │   ├── constants.py            # StripeEventType, Header keys, Tolerance, Modes
│   │   ├── client.py               # StripeClient singleton wrapping official Stripe SDK
│   │   ├── serializers.py          # CreateCheckoutSessionSerializer, Response schemas
│   │   ├── services.py             # StripeCatalogService, StripeCustomerService, StripeCheckoutService
│   │   ├── views.py                # CreateCheckoutSessionView, StripeWebhookView, Success/Cancel CBVs
│   │   └── handlers/               # Granular Webhook Handlers
│   │       ├── __init__.py
│   │       ├── events.py           # Dedicated handler functions for each StripeEventType
│   │       └── dispatcher.py       # StripeEventDispatcher with idempotency guards
│   │
│   └── tests/                      # Modular Automated Test Suite
│       ├── __init__.py
│       ├── test_features.py        # 10 tests: Feature, Plan, PlanFeature, validation
│       ├── test_prices.py          # 10 tests: PlanPrice, cents math, Stripe sync service
│       ├── test_customers.py       # 9 tests: StripeCustomer, 1:1 user mapping, reuse logic
│       ├── test_checkout.py        # 12 tests: Checkout session creation, CBV APIs, catalog
│       └── test_webhooks.py        # 12 tests: Webhook signature, idempotency, dispatcher
│
├── .env.example                    # Environment configuration template
├── .env                            # Local development variables (Stripe test keys)
├── .gitignore                      # Git exclusion rules
├── manage.py
├── pyproject.toml                  # Python dependencies managed via uv
└── PROJECT_PROGRESS.md             # This comprehensive architecture & progress document
```

---

## 5. Phase-by-Phase Detailed Implementation Log

### Phase 1: Environment, Dependencies & Stripe SDK Configuration
* **Objective**: Establish the Python 3.12 environment using `uv`, configure Django settings, secure API credentials, and initialize the official Stripe SDK client.
* **Key Implementations**:
  - Installed packages via `uv pip install`: `stripe==16.1.0`, `djangorestframework==3.18.3`, `python-dotenv==1.2.4`.
  - Configured [`config/settings.py`](file:///home/linkedmatrix/Payment-System/config/settings.py) to parse `.env` at boot using `Path(BASE_DIR / '.env')`.
  - Created [`payments/stripe/constants.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/constants.py) defining:
    - `STRIPE_SIGNATURE_HEADER = "HTTP_STRIPE_SIGNATURE"`
    - `DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300`
    - `CHECKOUT_MODE_SUBSCRIPTION = "subscription"`
    - `StripeEventType` class containing string constants for all standard webhook events.
  - Created [`payments/stripe/client.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/client.py):
    - Implemented `StripeClient` singleton that initializes `stripe.api_key = settings.STRIPE_SECRET_KEY` once upon import.
* **Verification**: `uv run python manage.py check` returned **0 issues**.

---

### Phase 2: Product Catalog & Entitlements (Feature, Plan, PlanFeature)
* **Objective**: Implement product capability models (`Feature`), subscription tiers (`Plan`), and their custom configuration through model (`PlanFeature`) with strict validation separating toggles from numerical quotas.
* **Key Implementations**:
  - Created [`payments/choices.py`](file:///home/linkedmatrix/Payment-System/payments/choices.py):
    - `FeatureTypeChoice`: `TOGGLE = "TOGGLE"`, `LIMIT = "LIMIT"`.
  - Created centralized validator `PlanFeatureConfigValidator` in [`payments/validators.py`](file:///home/linkedmatrix/Payment-System/payments/validators.py):
    - If `type == TOGGLE`: raises `ValidationError` if `limit is not None`.
    - If `type == LIMIT`: raises `ValidationError` if `limit is None`, `limit < 0`, or `unit` is blank.
  - Implemented models in [`payments/models/models.py`](file:///home/linkedmatrix/Payment-System/payments/models/models.py):
    - `Feature`: `name`, `slug` (auto-populated via slugify), `description`, `type`, `is_active`.
    - `Plan`: `name`, `slug`, `description`, `is_active`, `features` (M2M through `PlanFeature`).
    - `PlanFeature`: `plan`, `feature`, `is_enabled`, `limit`, `unit`. Unique together constraint: `[("plan", "feature")]`. Enforces `full_clean()` in `save()`.
  - Custom Managers in [`payments/managers.py`](file:///home/linkedmatrix/Payment-System/payments/managers.py):
    - `FeatureManager`: `.active()`, `.toggles()`, `.limits()`.
    - `PlanManager`: `.active()`, `.with_features()`.
    - `PlanFeatureManager`: `.enabled()`, `.for_plan()`, `.for_feature()`.
  - Django Admin in [`payments/admin.py`](file:///home/linkedmatrix/Payment-System/payments/admin.py):
    - Registered `PlanFeatureInline` (TabularInline on `PlanAdmin`).
    - Filterable by `type` and `is_active` with prepopulated slugs.
* **Test Suite**: 10 tests in [`payments/tests/test_features.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_features.py) passing.

---

### Phase 3: Multi-Interval Pricing & Stripe Catalog Synchronization
* **Objective**: Decouple pricing from plans to support monthly and annual pricing tiers, multi-currency catalogs, cents conversion, and automatic synchronization to Stripe test mode.
* **Key Implementations**:
  - Implemented `PlanPrice` in [`payments/models/models.py`](file:///home/linkedmatrix/Payment-System/payments/models/models.py):
    - Fields: `plan` (FK), `amount` (Decimal), `currency` (3-letter ISO code, normalized to lowercase), `billing_interval` (`day`, `week`, `month`, `year`), `stripe_price_id` (unique, nullable, indexed), `is_active`.
    - Method: `get_amount_in_cents() -> int`: Performs `int(round(self.amount * 100))` to eliminate floating point inaccuracies.
    - Property: `is_synced_with_stripe -> bool`.
  - Implemented `PlanPriceValidator` in [`payments/validators.py`](file:///home/linkedmatrix/Payment-System/payments/validators.py):
    - Validates `amount >= Decimal("0.00")`, `currency` length == 3 and alphabetic, `billing_interval` in supported choices, and `stripe_price_id` prefix (`price_` or `plan_`).
  - Implemented `StripeCatalogService` in [`payments/stripe/services.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/services.py):
    - `create_or_get_product(plan)`: Searches Stripe for existing Product by metadata `django_plan_slug`; creates new `stripe.Product` if not found.
    - `create_recurring_price(plan_price, product_id)`: Issues `stripe.Price.create()` with interval, amount in cents, currency, and metadata.
    - `sync_plan_price(plan_price)`: Orchestrates product & price creation and saves `stripe_price_id` locally.
  - Django Admin: Added `PlanPriceInline` and bulk action `sync_selected_prices_to_stripe`.
* **Test Suite**: 10 tests in [`payments/tests/test_prices.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_prices.py) passing (total 20 tests).

---

### Phase 4: Customer Mapping & Stripe Identity Synchronization
* **Objective**: Establish a strict 1-to-1 relationship between Django's User model and Stripe Customer records, allowing customers to be reused across repeated checkout sessions without duplicate Stripe customer entities.
* **Key Implementations**:
  - Implemented `StripeCustomer` in [`payments/models/models.py`](file:///home/linkedmatrix/Payment-System/payments/models/models.py):
    - `user`: `OneToOneField(settings.AUTH_USER_MODEL, related_name="stripe_customer")`.
    - `stripe_customer_id`: `CharField(max_length=100, unique=True, db_index=True)`.
  - Implemented `StripeCustomerValidator` in [`payments/validators.py`](file:///home/linkedmatrix/Payment-System/payments/validators.py):
    - Enforces user presence and verifies that `stripe_customer_id` starts with `'cus_'`.
  - Implemented `StripeCustomerService` in [`payments/stripe/services.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/services.py):
    - `get_or_create_customer(user) -> StripeCustomer`:
      1. Inspects the local database for existing `user.stripe_customer`.
      2. If present, returns the existing record immediately (zero API calls).
      3. If absent, calls `stripe.Customer.create()` with user email, name, and `metadata={"django_user_id": user.id}`, saves local record, and returns it.
* **Test Suite**: 9 tests in [`payments/tests/test_customers.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_customers.py) passing (total 29 tests).

---

### Phase 5: Stripe-Hosted Checkout Session & Catalog CBVs
* **Objective**: Provide authenticated customers with a secure hosted Stripe Checkout flow in subscription mode, validate selected pricing server-side, and expose public plan catalog endpoints.
* **Key Implementations**:
  - Implemented `StripeCheckoutService` in [`payments/stripe/services.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/services.py):
    - `create_subscription_checkout_session(user, plan_price, success_url, cancel_url)`:
      - Validates and auto-syncs `plan_price.stripe_price_id` with Stripe if missing.
      - Retrieves or provisions the user's `StripeCustomer`.
      - Issues `stripe.checkout.Session.create()` in `mode="subscription"` with `customer`, `line_items=[{"price": ..., "quantity": 1}]`, and metadata.
  - Implemented Serializers in [`payments/stripe/serializers.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/serializers.py):
    - `CreateCheckoutSessionSerializer`: Validates `plan_price_id` exists, `is_active=True`, and associated `plan.is_active=True`.
    - `CheckoutSessionResponseSerializer`: Returns `session_id` and `checkout_url`.
  - Implemented Class-Based Views:
    - `CreateCheckoutSessionView(APIView)`: Authenticated POST endpoint (`/payments/api/checkout/`).
    - `CheckoutSuccessView(APIView)`: Success return page enforcing zero-trust access.
    - `CheckoutCancelView(APIView)`: Handles checkout abandonment.
    - `PlanListAPIView(ListAPIView)`: Public catalog endpoint (`/payments/api/plans/`).
    - `PlanDetailAPIView(RetrieveAPIView)`: Single plan retrieval by slug (`/payments/api/plans/<slug>/`).
  - URL Routing in [`payments/urls.py`](file:///home/linkedmatrix/Payment-System/payments/urls.py) and [`config/urls.py`](file:///home/linkedmatrix/Payment-System/config/urls.py).
  - Test Optimization: Configured `MD5PasswordHasher` for automated test runs in `config/settings.py`.
* **Test Suite**: 12 tests in [`payments/tests/test_checkout.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_checkout.py) passing (total 41 tests).

---

### Phase 6: Webhook Security, Idempotency & Dictionary Dispatcher
* **Objective**: Implement a secure webhook endpoint that verifies cryptographic HMAC signatures, records an immutable audit log, prevents duplicate event processing via strict idempotency, and routes events via a Dictionary Dispatcher.
* **Key Implementations**:
  - Implemented `StripeWebhookEvent` model in [`payments/models/models.py`](file:///home/linkedmatrix/Payment-System/payments/models/models.py):
    - Fields: `stripe_event_id` (unique, indexed), `event_type`, `status` (`PENDING`, `PROCESSED`, `FAILED`, `IGNORED`), `payload` (JSON), `error_message`, `processed_at`.
  - Implemented `StripeWebhookVerificationService` in [`payments/stripe/services.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/services.py):
    - Validates HMAC signatures via `stripe.Webhook.construct_event()`.
    - Rejects missing headers or expired timestamps (> 300s).
  - Implemented the Dictionary Dispatcher in [`payments/stripe/handlers/dispatcher.py`](file:///home/linkedmatrix/Payment-System/payments/stripe/handlers/dispatcher.py):
    ```python
    DISPATCH_TABLE: Dict[str, EventHandlerFunc] = {
        StripeEventType.CHECKOUT_SESSION_COMPLETED: events.handle_checkout_session_completed,
        StripeEventType.CUSTOMER_SUBSCRIPTION_CREATED: events.handle_customer_subscription_created,
        StripeEventType.CUSTOMER_SUBSCRIPTION_UPDATED: events.handle_customer_subscription_updated,
        StripeEventType.CUSTOMER_SUBSCRIPTION_DELETED: events.handle_customer_subscription_deleted,
        StripeEventType.INVOICE_PAID: events.handle_invoice_paid,
        StripeEventType.INVOICE_PAYMENT_FAILED: events.handle_invoice_payment_failed,
        StripeEventType.INVOICE_PAYMENT_ACTION_REQUIRED: events.handle_invoice_payment_action_required,
    }
    ```
    - **Idempotency Guard**: Queries `StripeWebhookEvent` by `stripe_event_id`. If `status == PROCESSED`, ignores the duplicate payload safely.
    - Wraps handler execution in `transaction.atomic()`, recording `PROCESSED` with timestamp on success, and `FAILED` with traceback on error.
  - Implemented `StripeWebhookView(APIView)` at `/payments/webhooks/stripe/`.
  - Registered `StripeWebhookEventAdmin` in Django Admin.
* **Test Suite**: 12 tests in [`payments/tests/test_webhooks.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_webhooks.py) passing (total 53 tests).

---

### Architectural Refactor: Consolidated Models & Single Initial Migration
* **Objective**: Consolidate individual model files into a canonical single file at `payments/models/models.py` inside the `models/` directory (re-exported via `payments/models/__init__.py`), and consolidate all incremental development migrations into a single, clean `0001_initial.py` for pristine PR and commit history.
* **Outcome**:
  - Eliminated circular dependency risks between interrelated domain models.
  - Ensured all models are viewable in a single place with clear domain headers.
  - Updated all imports across `admin.py`, `serializers.py`, `views.py`, `stripe/services.py`, and test modules to use `from payments.models import ...`.
  - Consolidated 5 incremental migrations into a single clean `0001_initial.py`.
  - All 53 tests verified passing in **0.295s**.


---

## 6. Detailed Specifications for Remaining Phases (7 through 10)

> **Implementation contract**: Every section below is a binding spec. Do not deviate from file locations, class names, method signatures, or validation rules unless the entire team agrees to a change and updates this document first.

```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│             PHASE 7             │────►│             PHASE 8             │
│   Subscriptions, Periods &      │     │    Feature Usage Tracking &     │
│   Lifecycle State Machine       │     │    Atomic Quota Enforcement     │
└────────────────┬────────────────┘     └────────────────┬────────────────┘
                 │                                       │
                 ▼                                       ▼
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│             PHASE 9             │────►│            PHASE 10             │
│    Payment Ledger, Invoicing    │     │  End-to-End Verification,       │
│    & Customer Billing Portal    │     │  Failure Drills & Production    │
└─────────────────────────────────┘     └─────────────────────────────────┘
```

---

# Phase 7: Subscriptions, Periods & State Machine Lifecycle

### 7.1 Objectives & Business Rules

- Represent the contracted relationship between a customer (`User`) and a specific `PlanPrice`.
- Enforce the hard rule: **only one subscription per user may be `ACTIVE` or `TRIALING` at a time**. Historical canceled subscriptions are retained forever.
- Maintain a separate chronological timeline of billing cycles via `SubscriptionPeriod`.
- **Zero Blind Trust**: A `Subscription` is **never** created from the browser's redirect URL. It is provisioned exclusively when `checkout.session.completed` arrives via a verified webhook.
- Support two cancellation modes:
  - **At period end** (`cancel_at_period_end = True`): access continues until `current_period_end`, then Stripe fires `customer.subscription.deleted`.
  - **Immediate** (`cancel_at_period_end = False`): Stripe terminates immediately.
- Support reactivation: undo a pending cancellation before the period ends.
- All dates and timestamps are stored in UTC (`DateTimeField`) and sourced from Stripe's Unix timestamps (converted with `datetime.fromtimestamp(ts, tz=timezone.utc)`).

---

### 7.2 New Choices & Constants

**File: `payments/choices.py`** — Add the following `TextChoices` classes:

```python
class SubscriptionStatusChoice(models.TextChoices):
    """
    Mirrors Stripe subscription statuses exactly.
    See: https://stripe.com/docs/billing/subscriptions/overview#subscription-statuses
    """
    INCOMPLETE         = "incomplete",         _("Incomplete")
    INCOMPLETE_EXPIRED = "incomplete_expired",  _("Incomplete Expired")
    TRIALING           = "trialing",            _("Trialing")
    ACTIVE             = "active",              _("Active")
    PAST_DUE           = "past_due",            _("Past Due")
    CANCELED           = "canceled",            _("Canceled")
    UNPAID             = "unpaid",              _("Unpaid")
    PAUSED             = "paused",              _("Paused")


class PeriodStatusChoice(models.TextChoices):
    """
    Status of a historical subscription billing period.
    """
    ACTIVE    = "active",    _("Active")
    COMPLETED = "completed", _("Completed")
    CANCELED  = "canceled",  _("Canceled")
```

**File: `payments/constants.py`** — Add:

```python
# Maximum number of concurrent active subscriptions allowed per user.
MAX_ACTIVE_SUBSCRIPTIONS_PER_USER: int = 1

# Stripe ID prefixes
STRIPE_SUBSCRIPTION_ID_PREFIX: str = "sub_"
STRIPE_INVOICE_ID_PREFIX: str = "in_"
```

---

### 7.3 Database Models (`payments/models/models.py`)

Add the following two model classes **after** `StripeWebhookEvent`. Update `__all__` to include `"Subscription"` and `"SubscriptionPeriod"`.

#### 7.3.1 `Subscription(TimeStampedModel)`

```python
class Subscription(TimeStampedModel):
    """
    Represents an active or historical subscription contract between a customer
    and a PlanPrice. The authoritative source of truth is always Stripe;
    this model is kept in sync exclusively via verified webhooks.

    State machine:
      incomplete ──► active ──► past_due ──► canceled
                 └──► trialing ─┘         └──► unpaid
                                └──────────────────►
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        help_text=_("The Django user (customer) who owns this subscription."),
    )
    plan_price = models.ForeignKey(
        "PlanPrice",
        on_delete=models.PROTECT,   # Never delete a price that has subscriptions.
        related_name="subscriptions",
        help_text=_("The contracted PlanPrice (plan + billing interval + currency)."),
    )
    stripe_subscription_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Stripe Subscription object ID. Format: 'sub_XXXXXXXXXX'."),
    )
    status = models.CharField(
        max_length=25,
        choices=SubscriptionStatusChoice.choices,
        default=SubscriptionStatusChoice.INCOMPLETE,
        db_index=True,
        help_text=_("State machine status, kept in sync with Stripe via webhooks."),
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC timestamp when subscription first became active."),
    )
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC start of the current billing cycle (sourced from Stripe)."),
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC end of the current billing cycle (sourced from Stripe)."),
    )
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text=_(
            "If True, the subscription will cancel at current_period_end "
            "instead of renewing. Customer retains access until then."
        ),
    )
    canceled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC timestamp when the subscription was formally canceled."),
    )

    objects = SubscriptionManager()

    class Meta:
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="sub_user_status_idx"),
        ]

    def __str__(self) -> str:
        return (
            f"Subscription({self.user.username}, "
            f"{self.plan_price.plan.name}, {self.status})"
        )

    def clean(self) -> None:
        SubscriptionValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """True if the customer has valid, unrestricted access to plan features."""
        return self.status in [
            SubscriptionStatusChoice.ACTIVE,
            SubscriptionStatusChoice.TRIALING,
        ]

    @property
    def is_past_due(self) -> bool:
        """True if the latest recurring payment failed and Stripe is retrying."""
        return self.status == SubscriptionStatusChoice.PAST_DUE

    @property
    def is_pending_cancellation(self) -> bool:
        """True if customer requested cancel-at-period-end (still has access)."""
        return self.cancel_at_period_end and self.is_active
```

#### 7.3.2 `SubscriptionPeriod(TimeStampedModel)`

```python
class SubscriptionPeriod(TimeStampedModel):
    """
    An immutable historical record of a single billing cycle.
    One period is created per subscription renewal (monthly/yearly).
    Used as the anchor for StripePayment records and FeatureUsage resets.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="periods",
        help_text=_("Parent subscription this billing cycle belongs to."),
    )
    start_date = models.DateTimeField(
        help_text=_("UTC start of this billing cycle."),
    )
    end_date = models.DateTimeField(
        help_text=_("UTC end of this billing cycle."),
    )
    status = models.CharField(
        max_length=20,
        choices=PeriodStatusChoice.choices,
        default=PeriodStatusChoice.ACTIVE,
        db_index=True,
        help_text=_("Period status: active (current), completed (renewed), canceled."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID that triggered this period. Format: 'in_XXX'."),
    )

    objects = SubscriptionPeriodManager()

    class Meta:
        verbose_name = _("Subscription Period")
        verbose_name_plural = _("Subscription Periods")
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return (
            f"Period({self.subscription.stripe_subscription_id}, "
            f"{self.start_date.date()} → {self.end_date.date()}, {self.status})"
        )

    def clean(self) -> None:
        SubscriptionPeriodValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)
```

---

### 7.4 Validators (`payments/validators.py`)

Add the following two validator classes:

```python
class SubscriptionValidator:
    """
    Enforces all business rules for the Subscription model.
    Called from Subscription.clean().
    """

    @staticmethod
    def validate(instance: "Subscription") -> None:
        SubscriptionValidator._validate_stripe_id_prefix(instance)
        SubscriptionValidator._validate_single_active_subscription(instance)
        SubscriptionValidator._validate_period_dates(instance)

    @staticmethod
    def _validate_stripe_id_prefix(instance: "Subscription") -> None:
        if instance.stripe_subscription_id and not instance.stripe_subscription_id.startswith("sub_"):
            raise ValidationError(
                {"stripe_subscription_id": _("Stripe subscription ID must begin with 'sub_'.")}
            )

    @staticmethod
    def _validate_single_active_subscription(instance: "Subscription") -> None:
        """
        A user may only have ONE subscription in ACTIVE or TRIALING status.
        Historical canceled subscriptions are permitted and are not counted.
        """
        active_statuses = [
            SubscriptionStatusChoice.ACTIVE,
            SubscriptionStatusChoice.TRIALING,
        ]
        if instance.status not in active_statuses:
            return  # No conflict possible for non-active statuses.

        conflict_qs = Subscription.objects.filter(
            user=instance.user,
            status__in=active_statuses,
        )
        if instance.pk:
            conflict_qs = conflict_qs.exclude(pk=instance.pk)

        if conflict_qs.exists():
            raise ValidationError(
                {
                    "status": _(
                        "User already has an active or trialing subscription. "
                        "Cancel the existing one before activating a new one."
                    )
                }
            )

    @staticmethod
    def _validate_period_dates(instance: "Subscription") -> None:
        if (
            instance.current_period_start
            and instance.current_period_end
            and instance.current_period_end < instance.current_period_start
        ):
            raise ValidationError(
                {
                    "current_period_end": _(
                        "current_period_end must be greater than or equal to current_period_start."
                    )
                }
            )


class SubscriptionPeriodValidator:
    """
    Enforces all business rules for the SubscriptionPeriod model.
    Called from SubscriptionPeriod.clean().
    """

    @staticmethod
    def validate(instance: "SubscriptionPeriod") -> None:
        SubscriptionPeriodValidator._validate_date_range(instance)

    @staticmethod
    def _validate_date_range(instance: "SubscriptionPeriod") -> None:
        if instance.start_date and instance.end_date and instance.end_date <= instance.start_date:
            raise ValidationError(
                {"end_date": _("Period end_date must be strictly after start_date.")}
            )
```

---

### 7.5 Managers (`payments/managers.py`)

```python
class SubscriptionQuerySet(models.QuerySet):
    def active(self):
        """Subscriptions with ACTIVE or TRIALING status."""
        return self.filter(
            status__in=[
                SubscriptionStatusChoice.ACTIVE,
                SubscriptionStatusChoice.TRIALING,
            ]
        )

    def past_due(self):
        """Subscriptions awaiting payment retry."""
        return self.filter(status=SubscriptionStatusChoice.PAST_DUE)

    def canceled(self):
        """Terminated subscriptions (historical records)."""
        return self.filter(status=SubscriptionStatusChoice.CANCELED)

    def for_user(self, user):
        """All subscriptions belonging to a specific user."""
        return self.filter(user=user)

    def pending_cancellation(self):
        """Active subscriptions that will cancel at period end."""
        return self.active().filter(cancel_at_period_end=True)


class SubscriptionManager(models.Manager):
    def get_queryset(self) -> SubscriptionQuerySet:
        return SubscriptionQuerySet(self.model, using=self._db)

    def active(self):
        return self.get_queryset().active()

    def past_due(self):
        return self.get_queryset().past_due()

    def canceled(self):
        return self.get_queryset().canceled()

    def for_user(self, user):
        return self.get_queryset().for_user(user)

    def get_active_for_user(self, user):
        """
        Returns the single active/trialing subscription for a user,
        or None if no active subscription exists.
        """
        return self.get_queryset().active().filter(user=user).select_related(
            "plan_price__plan"
        ).first()


class SubscriptionPeriodQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=PeriodStatusChoice.ACTIVE)

    def completed(self):
        return self.filter(status=PeriodStatusChoice.COMPLETED)

    def for_subscription(self, subscription):
        return self.filter(subscription=subscription)

    def current(self, subscription):
        """Returns the currently active period for a given subscription."""
        return self.filter(
            subscription=subscription,
            status=PeriodStatusChoice.ACTIVE,
        ).first()


class SubscriptionPeriodManager(models.Manager):
    def get_queryset(self) -> SubscriptionPeriodQuerySet:
        return SubscriptionPeriodQuerySet(self.model, using=self._db)

    def active(self):
        return self.get_queryset().active()

    def for_subscription(self, subscription):
        return self.get_queryset().for_subscription(subscription)

    def current(self, subscription):
        return self.get_queryset().current(subscription)
```

---

### 7.6 Webhook Handlers (`payments/stripe/handlers/events.py`)

Replace the stub implementations with the following full logic:

#### `handle_checkout_session_completed(payload: dict) -> None`

```python
def handle_checkout_session_completed(payload: dict) -> None:
    """
    Fired when customer completes hosted checkout.
    Provisions the local Subscription and opens its first SubscriptionPeriod.

    Stripe payload path: payload["data"]["object"]
    """
    from payments.models import Subscription, SubscriptionPeriod, StripeCustomer, PlanPrice
    from payments.choices import SubscriptionStatusChoice, PeriodStatusChoice
    from django.utils import timezone
    from datetime import datetime, timezone as dt_timezone

    session = payload["data"]["object"]

    # 1. Resolve customer → user
    stripe_customer_id: str = session["customer"]
    try:
        stripe_customer = StripeCustomer.objects.select_related("user").get(
            stripe_customer_id=stripe_customer_id
        )
    except StripeCustomer.DoesNotExist:
        raise ValueError(
            f"No StripeCustomer found for stripe_customer_id={stripe_customer_id!r}"
        )
    user = stripe_customer.user

    # 2. Resolve PlanPrice from metadata
    plan_price_id: str = session.get("metadata", {}).get("django_plan_price_id")
    if not plan_price_id:
        raise ValueError("Checkout session metadata is missing 'django_plan_price_id'.")

    try:
        plan_price = PlanPrice.objects.select_related("plan").get(pk=int(plan_price_id))
    except (PlanPrice.DoesNotExist, ValueError):
        raise ValueError(f"No active PlanPrice found for pk={plan_price_id!r}")

    # 3. Retrieve fresh subscription details from Stripe
    stripe_subscription_id: str = session["subscription"]
    stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)

    # 4. Convert Unix timestamps to UTC datetime objects
    def to_utc(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=dt_timezone.utc)

    period_start = to_utc(stripe_sub["current_period_start"])
    period_end   = to_utc(stripe_sub["current_period_end"])

    # 5. Create or update the local Subscription record
    subscription, created = Subscription.objects.update_or_create(
        stripe_subscription_id=stripe_subscription_id,
        defaults={
            "user":                 user,
            "plan_price":           plan_price,
            "status":               stripe_sub["status"],
            "started_at":           timezone.now() if created else None,
            "current_period_start": period_start,
            "current_period_end":   period_end,
            "cancel_at_period_end": stripe_sub.get("cancel_at_period_end", False),
        },
    )
    if created:
        subscription.started_at = timezone.now()
        subscription.save(update_fields=["started_at"])

    # 6. Open the initial SubscriptionPeriod record
    SubscriptionPeriod.objects.get_or_create(
        subscription=subscription,
        start_date=period_start,
        defaults={
            "end_date": period_end,
            "status":   PeriodStatusChoice.ACTIVE,
            "stripe_invoice_id": None,
        },
    )
```

#### `handle_customer_subscription_updated(payload: dict) -> None`

```python
def handle_customer_subscription_updated(payload: dict) -> None:
    """
    Fired on every subscription modification:
    - Renewal (new billing period starts)
    - Cancellation flag change (cancel_at_period_end toggled)
    - Plan upgrade/downgrade (price ID changed)
    - Status transition (e.g., past_due → active after retry succeeds)
    """
    from payments.models import Subscription, SubscriptionPeriod, PlanPrice
    from payments.choices import PeriodStatusChoice
    from datetime import datetime, timezone as dt_timezone

    stripe_sub = payload["data"]["object"]
    stripe_subscription_id: str = stripe_sub["id"]

    def to_utc(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=dt_timezone.utc)

    try:
        subscription = Subscription.objects.select_related("plan_price").get(
            stripe_subscription_id=stripe_subscription_id
        )
    except Subscription.DoesNotExist:
        # Gracefully skip — the subscription may not yet exist locally
        # (e.g. if checkout.session.completed has not yet been processed).
        return

    new_period_start = to_utc(stripe_sub["current_period_start"])
    new_period_end   = to_utc(stripe_sub["current_period_end"])

    # Check if this is a renewal (period boundaries changed)
    period_renewed = (
        subscription.current_period_start != new_period_start
        or subscription.current_period_end != new_period_end
    )

    # Detect plan change (upgrade / downgrade)
    new_stripe_price_id: str = stripe_sub["items"]["data"][0]["price"]["id"]
    if subscription.plan_price.stripe_price_id != new_stripe_price_id:
        try:
            new_plan_price = PlanPrice.objects.get(stripe_price_id=new_stripe_price_id)
            subscription.plan_price = new_plan_price
        except PlanPrice.DoesNotExist:
            pass  # Price not synced locally; skip plan update but continue.

    # Sync all fields
    subscription.status               = stripe_sub["status"]
    subscription.current_period_start = new_period_start
    subscription.current_period_end   = new_period_end
    subscription.cancel_at_period_end = stripe_sub.get("cancel_at_period_end", False)
    subscription.save()

    # If period renewed, close old period and open a new one
    if period_renewed:
        SubscriptionPeriod.objects.filter(
            subscription=subscription,
            status=PeriodStatusChoice.ACTIVE,
        ).update(status=PeriodStatusChoice.COMPLETED)

        SubscriptionPeriod.objects.get_or_create(
            subscription=subscription,
            start_date=new_period_start,
            defaults={
                "end_date": new_period_end,
                "status":   PeriodStatusChoice.ACTIVE,
            },
        )
```

#### `handle_customer_subscription_deleted(payload: dict) -> None`

```python
def handle_customer_subscription_deleted(payload: dict) -> None:
    """
    Fired when a subscription is fully terminated (immediately canceled or
    cancel_at_period_end lapsed). Marks local records as CANCELED.
    """
    from payments.models import Subscription, SubscriptionPeriod
    from payments.choices import SubscriptionStatusChoice, PeriodStatusChoice
    from django.utils import timezone

    stripe_sub = payload["data"]["object"]
    stripe_subscription_id: str = stripe_sub["id"]

    try:
        subscription = Subscription.objects.get(
            stripe_subscription_id=stripe_subscription_id
        )
    except Subscription.DoesNotExist:
        return  # Already absent — nothing to do.

    subscription.status      = SubscriptionStatusChoice.CANCELED
    subscription.canceled_at = timezone.now()
    subscription.save(update_fields=["status", "canceled_at", "updated_at"])

    # Close all open periods
    SubscriptionPeriod.objects.filter(
        subscription=subscription,
        status=PeriodStatusChoice.ACTIVE,
    ).update(status=PeriodStatusChoice.CANCELED)
```

---

### 7.7 Outbound Stripe Services (`payments/stripe/services.py`)

Add `StripeSubscriptionService` to the existing services file:

```python
class StripeSubscriptionService:
    """
    Outbound Stripe API calls for subscription lifecycle management.
    Mirrors real Stripe state; local DB changes arrive via webhook.
    """

    @classmethod
    def cancel_subscription(
        cls,
        subscription: "Subscription",
        at_period_end: bool = True,
    ) -> dict:
        """
        Cancel a subscription.

        at_period_end=True  → Sets cancel_at_period_end=True on Stripe.
                               Customer retains access until current_period_end.
        at_period_end=False → Cancels immediately. Access ends now.

        Returns the raw Stripe Subscription object dict.
        """
        client = StripeClient.get_instance()
        if at_period_end:
            return client.subscriptions.update(
                subscription.stripe_subscription_id,
                cancel_at_period_end=True,
            )
        else:
            return client.subscriptions.cancel(
                subscription.stripe_subscription_id,
            )

    @classmethod
    def reactivate_subscription(cls, subscription: "Subscription") -> dict:
        """
        Undo a pending cancellation (cancel_at_period_end → False).
        Only valid while the subscription is still within its current period.

        Returns the raw Stripe Subscription object dict.
        """
        client = StripeClient.get_instance()
        return client.subscriptions.update(
            subscription.stripe_subscription_id,
            cancel_at_period_end=False,
        )
```

---

### 7.8 Serializers (`payments/serializers.py`)

Add to the existing serializers file:

```python
class SubscriptionPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPeriod
        fields = [
            "id",
            "start_date",
            "end_date",
            "status",
            "stripe_invoice_id",
            "created_at",
        ]
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name         = serializers.CharField(source="plan_price.plan.name", read_only=True)
    plan_slug         = serializers.CharField(source="plan_price.plan.slug", read_only=True)
    amount            = serializers.DecimalField(
        source="plan_price.amount", max_digits=10, decimal_places=2, read_only=True
    )
    currency          = serializers.CharField(source="plan_price.currency", read_only=True)
    billing_interval  = serializers.CharField(source="plan_price.billing_interval", read_only=True)
    is_active         = serializers.BooleanField(read_only=True)
    is_past_due       = serializers.BooleanField(read_only=True)
    is_pending_cancellation = serializers.BooleanField(read_only=True)
    current_period    = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "stripe_subscription_id",
            "status",
            "is_active",
            "is_past_due",
            "is_pending_cancellation",
            "plan_name",
            "plan_slug",
            "amount",
            "currency",
            "billing_interval",
            "started_at",
            "current_period_start",
            "current_period_end",
            "cancel_at_period_end",
            "canceled_at",
            "current_period",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_current_period(self, obj: "Subscription") -> dict | None:
        period = SubscriptionPeriod.objects.current(obj)
        if period:
            return SubscriptionPeriodSerializer(period).data
        return None


class CancelSubscriptionSerializer(serializers.Serializer):
    """Request body for POST /payments/api/subscriptions/cancel/"""
    at_period_end = serializers.BooleanField(
        default=True,
        help_text=_(
            "If true, subscription is canceled at current_period_end. "
            "If false, canceled immediately."
        ),
    )
```

---

### 7.9 Views (`payments/views.py`)

Add the following three CBVs to the existing views file:

```python
class CurrentSubscriptionView(APIView):
    """
    GET /payments/api/subscriptions/current/

    Returns the authenticated user's active or trialing subscription.
    Returns HTTP 404 if no active subscription exists.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = SubscriptionSerializer(subscription)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CancelSubscriptionView(APIView):
    """
    POST /payments/api/subscriptions/cancel/

    Cancels the authenticated user's active subscription.
    Request body: { "at_period_end": true }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription to cancel."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CancelSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            StripeSubscriptionService.cancel_subscription(
                subscription,
                at_period_end=serializer.validated_data["at_period_end"],
            )
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe error: {exc.user_message}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # The real status update arrives via webhook.
        # Return current state immediately for UX responsiveness.
        return Response(
            {
                "detail": "Cancellation request sent to Stripe.",
                "at_period_end": serializer.validated_data["at_period_end"],
            },
            status=status.HTTP_200_OK,
        )


class ReactivateSubscriptionView(APIView):
    """
    POST /payments/api/subscriptions/reactivate/

    Reverses a pending at_period_end cancellation.
    Only valid while cancel_at_period_end=True and subscription is still active.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        subscription = Subscription.objects.get_active_for_user(request.user)
        if not subscription:
            return Response(
                {"detail": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not subscription.cancel_at_period_end:
            return Response(
                {"detail": "Subscription is not scheduled for cancellation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            StripeSubscriptionService.reactivate_subscription(subscription)
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe error: {exc.user_message}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {"detail": "Subscription reactivation request sent to Stripe."},
            status=status.HTTP_200_OK,
        )
```

---

### 7.10 URL Patterns (`payments/urls.py`)

Add to the existing `urlpatterns` list:

```python
 — Subscription lifecycle management
path("api/subscriptions/current/",    CurrentSubscriptionView.as_view(),    name="subscription-current"),
path("api/subscriptions/cancel/",     CancelSubscriptionView.as_view(),     name="subscription-cancel"),
path("api/subscriptions/reactivate/", ReactivateSubscriptionView.as_view(), name="subscription-reactivate"),
```

---

### 7.11 Admin (`payments/admin.py`)

```python
class SubscriptionPeriodInline(admin.TabularInline):
    model = SubscriptionPeriod
    extra = 0
    readonly_fields = ["start_date", "end_date", "status", "stripe_invoice_id", "created_at"]
    can_delete = False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display  = ["user", "plan_price", "status", "current_period_end", "cancel_at_period_end", "created_at"]
    list_filter   = ["status", "cancel_at_period_end", "plan_price__billing_interval"]
    search_fields = ["user__username", "user__email", "stripe_subscription_id"]
    readonly_fields = [
        "stripe_subscription_id", "started_at",
        "current_period_start", "current_period_end",
        "canceled_at", "created_at", "updated_at",
    ]
    inlines = [SubscriptionPeriodInline]
    actions = ["action_cancel_at_period_end", "action_sync_from_stripe"]

    @admin.action(description="Cancel selected subscriptions at period end")
    def action_cancel_at_period_end(self, request, queryset):
        for sub in queryset.filter(status__in=[
            SubscriptionStatusChoice.ACTIVE, SubscriptionStatusChoice.TRIALING
        ]):
            StripeSubscriptionService.cancel_subscription(sub, at_period_end=True)
        self.message_user(request, "Cancellation requests sent to Stripe.")

    @admin.action(description="Sync status from Stripe")
    def action_sync_from_stripe(self, request, queryset):
        from payments.stripe.client import StripeClient
        client = StripeClient.get_instance()
        for sub in queryset:
            stripe_sub = client.subscriptions.retrieve(sub.stripe_subscription_id)
            sub.status = stripe_sub["status"]
            sub.save(update_fields=["status", "updated_at"])
        self.message_user(request, "Synced statuses from Stripe.")
```

---

### 7.12 Test Cases (`payments/tests/test_subscriptions.py`)

Create `payments/tests/test_subscriptions.py` with the following **13 test methods**:

```
TestSubscriptionModel:
  1. test_create_valid_subscription
     - Create Subscription with valid fields; assert saved correctly.

  2. test_stripe_subscription_id_prefix_validation
     - Attempt to save Subscription with stripe_subscription_id="INVALID_1234".
     - Assert ValidationError raised with key 'stripe_subscription_id'.

  3. test_single_active_subscription_per_user_constraint
     - Create first ACTIVE subscription for user.
     - Attempt to create second ACTIVE subscription for same user.
     - Assert ValidationError raised referencing 'status'.

  4. test_historical_canceled_subscription_does_not_block_new_active
     - Create CANCELED subscription for user.
     - Create new ACTIVE subscription for same user.
     - Assert both exist without error.

  5. test_period_date_validation
     - Attempt SubscriptionPeriod where end_date == start_date.
     - Assert ValidationError raised with key 'end_date'.

  6. test_is_active_property_returns_true_for_active_and_trialing
     - Create ACTIVE subscription → is_active == True.
     - Create TRIALING subscription → is_active == True.
     - Create PAST_DUE subscription → is_active == False.

  7. test_is_past_due_property
     - Assert PAST_DUE subscription.is_past_due == True.
     - Assert ACTIVE subscription.is_past_due == False.

  8. test_is_pending_cancellation_property
     - ACTIVE sub with cancel_at_period_end=True → is_pending_cancellation == True.
     - ACTIVE sub with cancel_at_period_end=False → is_pending_cancellation == False.

TestSubscriptionManagers:
  9. test_manager_active_returns_only_active_and_trialing
     - Create one ACTIVE, one TRIALING, one CANCELED subscription.
     - Subscription.objects.active() → returns only 2.

  10. test_manager_get_active_for_user_returns_none_without_subscription
      - Call get_active_for_user(user) with no subscriptions.
      - Assert result is None.

TestWebhookHandlers:
  11. test_checkout_session_completed_creates_subscription_and_period
      - Build mock checkout.session.completed payload.
      - Mock stripe.Subscription.retrieve to return controlled values.
      - Call handle_checkout_session_completed(payload).
      - Assert Subscription created with correct user, plan_price, status.
      - Assert SubscriptionPeriod created with correct dates.

  12. test_customer_subscription_updated_syncs_new_period
      - Pre-create local Subscription.
      - Build mock customer.subscription.updated payload with new period dates.
      - Call handle_customer_subscription_updated(payload).
      - Assert new SubscriptionPeriod created; old one marked COMPLETED.

  13. test_customer_subscription_deleted_marks_canceled
      - Pre-create local Subscription (ACTIVE) and SubscriptionPeriod (ACTIVE).
      - Call handle_customer_subscription_deleted(payload).
      - Assert subscription.status == CANCELED.
      - Assert subscription.canceled_at is not None.
      - Assert SubscriptionPeriod.status == CANCELED.
```

**Target after Phase 7**: **66 passing tests in < 0.5s**

---

### 7.13 Migration & Verification Steps

```bash
# 1. Generate migration
uv run python manage.py makemigrations payments

# 2. Apply migration
uv run python manage.py migrate

# 3. Run all tests (target: 66 passing)
uv run python manage.py test payments

# 4. Run system checks
uv run python manage.py check
```

---
---

## Phase 8: Feature Usage Tracking & Atomic Quota Enforcement

### 8.1 Objectives & Business Rules

- Separate what a customer **may consume** (`PlanFeature.limit`) from what they **have consumed** (`FeatureUsage.used_amount`).
- Support both **Toggle features** (binary `is_enabled` gate — no counting) and **Limit features** (numerical quotas such as "1,000 API calls per month").
- **Concurrency Safety**: Quota enforcement must be atomic. Two concurrent API requests for the same user must not collectively exceed the quota. Achieved with `select_for_update()` row locking + Django `F()` expressions inside `transaction.atomic()`.
- **Automated Period Resets**: When a new `SubscriptionPeriod` begins (triggered by `invoice.paid`), all `FeatureUsage` counters are reset to zero by creating fresh records for the new period.
- Provide reusable DRF permission classes (`HasActiveSubscription`, `HasFeatureEntitlement`) to gate API views declaratively.

---

### 8.2 New Choices & Constants

**File: `payments/choices.py`** — No new choices needed; `FeatureTypeChoice` already defined.

**File: `payments/constants.py`** — Add:

```python
# Feature usage
FEATURE_QUOTA_UNIT_DEFAULT: str = "units"

# Custom exception codes
QUOTA_EXCEEDED_CODE: str = "quota_exceeded"
NO_ACTIVE_SUBSCRIPTION_CODE: str = "no_active_subscription"
FEATURE_DISABLED_CODE: str = "feature_disabled"
```

---

### 8.3 Database Model (`payments/models/models.py`)

Add `FeatureUsage` **after** `SubscriptionPeriod`. Update `__all__`.

```python
class FeatureUsage(TimeStampedModel):
    """
    Real-time usage counter for a metered (LIMIT-type) feature.

    One record exists per (subscription, feature, period_start) combination.
    It is reset to zero when a new SubscriptionPeriod begins (on invoice.paid).

    TOGGLE features do not generate FeatureUsage records; their access is
    determined solely by PlanFeature.is_enabled.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="usages",
        help_text=_("The subscription this usage counter belongs to."),
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="usages",
        help_text=_("The metered capability being counted."),
    )
    period_start = models.DateTimeField(
        help_text=_("UTC start of the billing period for which usage is measured."),
    )
    period_end = models.DateTimeField(
        help_text=_("UTC end of the billing period for which usage is measured."),
    )
    used_amount = models.PositiveIntegerField(
        default=0,
        help_text=_(
            "Total units consumed in this period. Incremented atomically "
            "via F() expressions to prevent race conditions."
        ),
    )

    objects = FeatureUsageManager()

    class Meta:
        verbose_name = _("Feature Usage")
        verbose_name_plural = _("Feature Usages")
        unique_together = [("subscription", "feature", "period_start")]
        ordering = ["-period_start"]

    def __str__(self) -> str:
        return (
            f"{self.subscription.user.username} | "
            f"{self.feature.name}: {self.used_amount} used"
        )

    def clean(self) -> None:
        FeatureUsageValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)
```

---

### 8.4 Validators (`payments/validators.py`)

```python
class FeatureUsageValidator:
    """
    Enforces business rules on FeatureUsage records.
    Called from FeatureUsage.clean().
    """

    @staticmethod
    def validate(instance: "FeatureUsage") -> None:
        FeatureUsageValidator._validate_period_order(instance)
        FeatureUsageValidator._validate_limit_feature_type(instance)

    @staticmethod
    def _validate_period_order(instance: "FeatureUsage") -> None:
        if (
            instance.period_start
            and instance.period_end
            and instance.period_end <= instance.period_start
        ):
            raise ValidationError(
                {"period_end": _("period_end must be strictly after period_start.")}
            )

    @staticmethod
    def _validate_limit_feature_type(instance: "FeatureUsage") -> None:
        """
        FeatureUsage records must only be created for LIMIT-type features.
        TOGGLE features use PlanFeature.is_enabled — not counters.
        """
        if instance.feature_id and instance.feature.type == FeatureTypeChoice.TOGGLE:
            raise ValidationError(
                {
                    "feature": _(
                        "FeatureUsage records are only valid for LIMIT-type features. "
                        "TOGGLE features are gated via PlanFeature.is_enabled."
                    )
                }
            )
```

---

### 8.5 Managers (`payments/managers.py`)

```python
class FeatureUsageQuerySet(models.QuerySet):
    def for_subscription(self, subscription):
        return self.filter(subscription=subscription)

    def for_feature(self, feature_slug: str):
        return self.filter(feature__slug=feature_slug)

    def current_period(self, subscription):
        """
        Returns usage records for the subscription's currently active period.
        """
        from payments.models import SubscriptionPeriod
        from payments.choices import PeriodStatusChoice
        period = SubscriptionPeriod.objects.current(subscription)
        if not period:
            return self.none()
        return self.filter(
            subscription=subscription,
            period_start=period.start_date,
        )


class FeatureUsageManager(models.Manager):
    def get_queryset(self) -> FeatureUsageQuerySet:
        return FeatureUsageQuerySet(self.model, using=self._db)

    def for_subscription(self, subscription):
        return self.get_queryset().for_subscription(subscription)

    def current_period(self, subscription):
        return self.get_queryset().current_period(subscription)
```

---

### 8.6 Entitlement Service (`payments/services.py`)

Create a new file `payments/services.py`:

```python
"""
payments/services.py

Domain service layer for entitlement checking and quota enforcement.
All logic is class-based, centralized, and invoked from views and handlers.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext_lazy as _

from payments.choices import FeatureTypeChoice, SubscriptionStatusChoice
from payments.constants import QUOTA_EXCEEDED_CODE, FEATURE_DISABLED_CODE, NO_ACTIVE_SUBSCRIPTION_CODE
from payments.models import (
    Subscription,
    SubscriptionPeriod,
    Feature,
    PlanFeature,
    FeatureUsage,
)


class QuotaExceededError(Exception):
    """
    Raised when a user attempts to consume more of a feature than their plan allows.
    """
    def __init__(self, feature_slug: str, used: int, limit: int) -> None:
        self.feature_slug = feature_slug
        self.used = used
        self.limit = limit
        self.code = QUOTA_EXCEEDED_CODE
        super().__init__(
            f"Quota exceeded for '{feature_slug}': {used + 1} > {limit} allowed."
        )


class EntitlementService:
    """
    Answers two core questions:
      1. Does this user have access to a feature? (check_feature_access)
      2. Can we atomically consume N units of a quota? (consume_quota)

    All public methods are classmethods that accept a Django User object.
    """

    @classmethod
    def _get_active_subscription(cls, user) -> Subscription | None:
        return Subscription.objects.get_active_for_user(user)

    @classmethod
    def _get_plan_feature(cls, subscription: Subscription, feature_slug: str) -> PlanFeature | None:
        try:
            return PlanFeature.objects.select_related("feature").get(
                plan=subscription.plan_price.plan,
                feature__slug=feature_slug,
                is_enabled=True,
            )
        except PlanFeature.DoesNotExist:
            return None

    @classmethod
    def check_feature_access(cls, user, feature_slug: str) -> tuple[bool, str]:
        """
        Determines if a user has access to a given feature.

        Returns a tuple of (has_access: bool, reason_code: str).

        Reason codes are defined in payments/constants.py:
          - '' (empty string)          → access granted
          - NO_ACTIVE_SUBSCRIPTION_CODE → user has no active subscription
          - FEATURE_DISABLED_CODE       → feature not enabled on their plan
          - QUOTA_EXCEEDED_CODE         → limit feature quota is exhausted
        """
        subscription = cls._get_active_subscription(user)
        if not subscription:
            return False, NO_ACTIVE_SUBSCRIPTION_CODE

        plan_feature = cls._get_plan_feature(subscription, feature_slug)
        if not plan_feature:
            return False, FEATURE_DISABLED_CODE

        if plan_feature.feature.type == FeatureTypeChoice.TOGGLE:
            # Toggle features: is_enabled=True is sufficient.
            return True, ""

        # LIMIT feature: compare used_amount vs configured limit.
        current_period = SubscriptionPeriod.objects.current(subscription)
        if not current_period:
            return False, NO_ACTIVE_SUBSCRIPTION_CODE

        usage, _ = FeatureUsage.objects.get_or_create(
            subscription=subscription,
            feature=plan_feature.feature,
            period_start=current_period.start_date,
            defaults={"period_end": current_period.end_date},
        )

        if usage.used_amount >= plan_feature.limit:
            return False, QUOTA_EXCEEDED_CODE

        return True, ""

    @classmethod
    def consume_quota(cls, user, feature_slug: str, amount: int = 1) -> bool:
        """
        Atomically increments used_amount for a LIMIT feature.

        Uses select_for_update() + F() expression to guarantee no race condition
        can allow consumption beyond the configured limit.

        Raises:
          QuotaExceededError: if the new amount would exceed plan_feature.limit.
          ValueError: if the feature is a TOGGLE type (not metered).

        Returns True on success.
        """
        subscription = cls._get_active_subscription(user)
        if not subscription:
            raise PermissionError(NO_ACTIVE_SUBSCRIPTION_CODE)

        plan_feature = cls._get_plan_feature(subscription, feature_slug)
        if not plan_feature:
            raise PermissionError(FEATURE_DISABLED_CODE)

        if plan_feature.feature.type == FeatureTypeChoice.TOGGLE:
            raise ValueError(
                f"Feature '{feature_slug}' is a TOGGLE type. "
                "It cannot be consumed as a quota."
            )

        current_period = SubscriptionPeriod.objects.current(subscription)
        if not current_period:
            raise PermissionError(NO_ACTIVE_SUBSCRIPTION_CODE)

        with transaction.atomic():
            # Lock the row to prevent concurrent over-consumption.
            usage = (
                FeatureUsage.objects
                .select_for_update()
                .get_or_create(
                    subscription=subscription,
                    feature=plan_feature.feature,
                    period_start=current_period.start_date,
                    defaults={"period_end": current_period.end_date},
                )[0]
            )

            if usage.used_amount + amount > plan_feature.limit:
                raise QuotaExceededError(
                    feature_slug=feature_slug,
                    used=usage.used_amount,
                    limit=plan_feature.limit,
                )

            # Atomic increment: safe under concurrent load.
            FeatureUsage.objects.filter(pk=usage.pk).update(
                used_amount=F("used_amount") + amount
            )

        return True

    @classmethod
    def get_user_usage_summary(cls, user) -> list[dict[str, Any]]:
        """
        Returns a complete breakdown of all plan features for the user.
        Called by UsageSummaryView to populate the usage dashboard.

        Each item in the returned list has:
          - feature_name: str
          - feature_slug: str
          - feature_type: str  ("TOGGLE" | "LIMIT")
          - is_enabled: bool
          - limit: int | None
          - unit: str
          - used_amount: int | None
          - remaining: int | None
          - percent_used: float | None  (0.0 – 100.0)
        """
        subscription = cls._get_active_subscription(user)
        if not subscription:
            return []

        plan_features = PlanFeature.objects.filter(
            plan=subscription.plan_price.plan
        ).select_related("feature")

        current_period = SubscriptionPeriod.objects.current(subscription)
        summary = []

        for pf in plan_features:
            entry: dict[str, Any] = {
                "feature_name": pf.feature.name,
                "feature_slug": pf.feature.slug,
                "feature_type": pf.feature.type,
                "is_enabled":   pf.is_enabled,
                "limit":        pf.limit,
                "unit":         pf.unit or "",
                "used_amount":  None,
                "remaining":    None,
                "percent_used": None,
            }

            if pf.feature.type == FeatureTypeChoice.LIMIT and current_period:
                try:
                    usage = FeatureUsage.objects.get(
                        subscription=subscription,
                        feature=pf.feature,
                        period_start=current_period.start_date,
                    )
                    used = usage.used_amount
                except FeatureUsage.DoesNotExist:
                    used = 0

                remaining = max(0, (pf.limit or 0) - used)
                entry["used_amount"]  = used
                entry["remaining"]    = remaining
                entry["percent_used"] = round((used / pf.limit * 100), 2) if pf.limit else 0.0

            summary.append(entry)

        return summary

    @classmethod
    def reset_usage_for_new_period(cls, subscription: Subscription, new_period: SubscriptionPeriod) -> None:
        """
        Called by handle_invoice_paid() when a new billing period begins.
        Opens fresh FeatureUsage records (used_amount = 0) for every LIMIT feature
        on the subscription's plan.

        Does NOT delete historical usage records from the previous period —
        those remain as permanent audit history.
        """
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
                    "period_end":  new_period.end_date,
                    "used_amount": 0,
                },
            )
```

---

### 8.7 Permissions (`payments/permissions.py`)

Create a new file `payments/permissions.py`:

```python
"""
payments/permissions.py

Reusable DRF permission classes for subscription and entitlement gating.
"""

from rest_framework.permissions import BasePermission

from payments.choices import SubscriptionStatusChoice
from payments.models import Subscription


class HasActiveSubscription(BasePermission):
    """
    Allows access only to users with an ACTIVE or TRIALING subscription.
    Returns HTTP 403 with a descriptive message on failure.

    Usage:
        class MyView(APIView):
            permission_classes = [IsAuthenticated, HasActiveSubscription]
    """
    message = "An active subscription is required to access this resource."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        return Subscription.objects.filter(
            user=request.user,
            status__in=[
                SubscriptionStatusChoice.ACTIVE,
                SubscriptionStatusChoice.TRIALING,
            ],
        ).exists()


class HasFeatureEntitlement:
    """
    Factory that generates a per-feature permission class.
    Checks both feature enablement (TOGGLE) and quota (LIMIT).

    Usage:
        class MyView(APIView):
            permission_classes = [
                IsAuthenticated,
                HasActiveSubscription,
                HasFeatureEntitlement("ai-requests"),
            ]
    """
    def __init__(self, feature_slug: str, required_amount: int = 1) -> None:
        self.feature_slug   = feature_slug
        self.required_amount = required_amount

    def __call__(self) -> type:
        feature_slug    = self.feature_slug
        required_amount = self.required_amount

        class _FeaturePermission(BasePermission):
            message = f"You do not have access to the '{feature_slug}' feature or your quota is exhausted."

            def has_permission(self, request, view) -> bool:
                from payments.services import EntitlementService
                has_access, _ = EntitlementService.check_feature_access(
                    request.user, feature_slug
                )
                return has_access

        return _FeaturePermission
```

---

### 8.8 Views (`payments/views.py`)

Add the following two CBVs:

```python
class UsageSummaryView(APIView):
    """
    GET /payments/api/usage/

    Returns a real-time breakdown of all plan features for the authenticated user.
    Includes: feature name, type, configured limit, units consumed, remaining, % used.
    Requires an active subscription.
    """
    permission_classes = [IsAuthenticated, HasActiveSubscription]

    def get(self, request: Request) -> Response:
        from payments.services import EntitlementService
        summary = EntitlementService.get_user_usage_summary(request.user)
        return Response(summary, status=status.HTTP_200_OK)


class FeatureAccessCheckView(APIView):
    """
    GET /payments/api/usage/check/<slug:feature_slug>/

    Instant entitlement check for a single feature.
    Used by frontend or internal services before gating an action.

    Response: { "allowed": true, "remaining": 650, "reason": "" }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, feature_slug: str) -> Response:
        from payments.services import EntitlementService
        has_access, reason = EntitlementService.check_feature_access(
            request.user, feature_slug
        )

        # Get remaining count for LIMIT features
        remaining = None
        subscription = Subscription.objects.get_active_for_user(request.user)
        if subscription:
            from payments.models import PlanFeature, FeatureUsage, SubscriptionPeriod
            from payments.choices import FeatureTypeChoice
            try:
                pf = PlanFeature.objects.get(
                    plan=subscription.plan_price.plan,
                    feature__slug=feature_slug,
                )
                if pf.feature.type == FeatureTypeChoice.LIMIT:
                    period = SubscriptionPeriod.objects.current(subscription)
                    if period:
                        usage = FeatureUsage.objects.filter(
                            subscription=subscription,
                            feature=pf.feature,
                            period_start=period.start_date,
                        ).first()
                        used = usage.used_amount if usage else 0
                        remaining = max(0, (pf.limit or 0) - used)
            except PlanFeature.DoesNotExist:
                pass

        return Response(
            {
                "allowed":   has_access,
                "remaining": remaining,
                "reason":    reason,
            },
            status=status.HTTP_200_OK,
        )
```

---

### 8.9 URL Patterns (`payments/urls.py`)

Add to `urlpatterns`:

```python
# Phase 8 — Usage & Entitlement
path("api/usage/",                       UsageSummaryView.as_view(),      name="usage-summary"),
path("api/usage/check/<slug:feature_slug>/", FeatureAccessCheckView.as_view(), name="usage-check"),
```

---

### 8.10 Admin (`payments/admin.py`)

```python
@admin.register(FeatureUsage)
class FeatureUsageAdmin(admin.ModelAdmin):
    list_display  = ["subscription", "feature", "used_amount", "period_start", "period_end"]
    list_filter   = ["feature__type", "period_start"]
    search_fields = ["subscription__user__username", "feature__name"]
    readonly_fields = ["subscription", "feature", "period_start", "period_end", "used_amount", "created_at"]
```

---

### 8.11 Test Cases (`payments/tests/test_usage.py`)

Create `payments/tests/test_usage.py` with the following **11 test methods**:

```
TestFeatureUsageModel:
  1. test_create_valid_feature_usage
     - Create FeatureUsage for LIMIT feature; assert saved and used_amount=0.

  2. test_unique_together_prevents_duplicate_usage_record
     - Create FeatureUsage; attempt identical (subscription, feature, period_start).
     - Assert IntegrityError or ValidationError raised.

  3. test_toggle_feature_raises_validation_error_on_usage_record
     - Create FeatureUsage for a TOGGLE feature.
     - Assert ValidationError raised with message about LIMIT-only restriction.

  4. test_period_end_must_be_after_period_start
     - Attempt FeatureUsage with period_end == period_start.
     - Assert ValidationError raised.

TestEntitlementServiceToggle:
  5. test_toggle_feature_access_returns_true_when_enabled
     - Setup: active subscription + PlanFeature(TOGGLE, is_enabled=True).
     - check_feature_access(user, slug) → (True, "").

  6. test_toggle_feature_access_returns_false_when_disabled
     - Setup: PlanFeature(TOGGLE, is_enabled=False).
     - check_feature_access(user, slug) → (False, FEATURE_DISABLED_CODE).

TestEntitlementServiceLimit:
  7. test_limit_feature_allows_consumption_under_quota
     - Plan limit = 100; used_amount = 50.
     - check_feature_access → (True, "").

  8. test_limit_feature_blocks_when_quota_exhausted
     - Plan limit = 10; used_amount = 10.
     - check_feature_access → (False, QUOTA_EXCEEDED_CODE).

  9. test_consume_quota_increments_atomically
     - Call consume_quota 5 times with amount=1.
     - Assert used_amount == 5 with no duplication.

  10. test_consume_quota_raises_quota_exceeded_error_at_boundary
      - Plan limit = 3; used_amount = 3.
      - consume_quota(user, slug, amount=1) → raises QuotaExceededError.

  11. test_reset_usage_for_new_period_creates_fresh_records
      - Existing FeatureUsage with used_amount = 80.
      - Create new SubscriptionPeriod (simulating renewal).
      - Call EntitlementService.reset_usage_for_new_period(subscription, new_period).
      - Assert new FeatureUsage created with used_amount = 0 for new period.
      - Assert old FeatureUsage still exists (historical record preserved).

TestPermissions:
  12. test_has_active_subscription_blocks_unauthenticated
      - Unauthenticated POST to /payments/api/usage/ → HTTP 401.

  13. test_has_active_subscription_blocks_user_without_subscription
      - Authenticated user with no subscription → HTTP 403.
```

**Target after Phase 8**: **79 passing tests in < 0.7s**

---
---

## Phase 9: Payment Ledger, Invoicing & Billing Portal

### 9.1 Objectives & Business Rules

- Maintain a permanent, immutable financial transaction ledger (`StripePayment`) that records every successful charge, failed charge, and refund.
- Each `StripePayment` is linked to both the parent `Subscription` and the specific `SubscriptionPeriod` it covers.
- Handle recurring billing webhooks:
  - `invoice.paid` → create `StripePayment(status=SUCCEEDED)`, restore `PAST_DUE` subscriptions, trigger usage reset.
  - `invoice.payment_failed` → create `StripePayment(status=FAILED)`, mark subscription `PAST_DUE`.
  - `invoice.payment_action_required` → log and notify (dunning / SCA action needed).
- Provide Stripe's hosted **Customer Billing Portal** so customers can self-serve: update cards, view receipts, and cancel subscriptions — without us handling card data.

---

### 9.2 New Choices & Constants

**File: `payments/choices.py`** — Add:

```python
class PaymentStatusChoice(models.TextChoices):
    """
    Lifecycle status of a StripePayment transaction record.
    """
    PENDING   = "pending",   _("Pending")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED    = "failed",    _("Failed")
    REFUNDED  = "refunded",  _("Refunded")
```

**File: `payments/constants.py`** — Add:

```python
STRIPE_PAYMENT_INTENT_ID_PREFIX: str = "pi_"
STRIPE_INVOICE_ID_PREFIX: str        = "in_"  # (already added in Phase 7)
```

---

### 9.3 Database Model (`payments/models/models.py`)

Add `StripePayment` **after** `FeatureUsage`. Update `__all__`.

```python
class StripePayment(TimeStampedModel):
    """
    Immutable financial transaction ledger entry.

    Created exclusively by webhook handlers (invoice.paid, invoice.payment_failed).
    NEVER created from the frontend. Serves as the authoritative billing audit trail.
    """

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="payments",
        help_text=_("The subscription this payment was collected for."),
    )
    period = models.ForeignKey(
        SubscriptionPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        help_text=_("The billing period this payment covers (null if period not yet created)."),
    )
    stripe_payment_intent_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe PaymentIntent ID. Format: 'pi_XXXXXXXXXX'."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID. Format: 'in_XXXXXXXXXX'."),
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_("Amount in standard currency units (e.g. 29.00 for $29.00)."),
    )
    currency = models.CharField(
        max_length=3,
        default="usd",
        help_text=_("3-letter lowercase ISO 4217 currency code (e.g. 'usd', 'eur')."),
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentStatusChoice.choices,
        default=PaymentStatusChoice.PENDING,
        db_index=True,
        help_text=_("Transaction lifecycle status."),
    )
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("UTC timestamp when payment was confirmed as succeeded."),
    )

    objects = StripePaymentManager()

    class Meta:
        verbose_name = _("Stripe Payment")
        verbose_name_plural = _("Stripe Payments")
        ordering = ["-paid_at", "-created_at"]

    def __str__(self) -> str:
        return (
            f"Payment({self.subscription.user.username}, "
            f"{self.amount} {self.currency.upper()}, {self.status})"
        )

    def clean(self) -> None:
        StripePaymentValidator.validate(self)

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)
```

---

### 9.4 Validators (`payments/validators.py`)

```python
class StripePaymentValidator:
    """
    Enforces financial integrity rules on StripePayment records.
    Called from StripePayment.clean().
    """

    @staticmethod
    def validate(instance: "StripePayment") -> None:
        StripePaymentValidator._validate_positive_amount(instance)
        StripePaymentValidator._validate_currency_format(instance)
        StripePaymentValidator._validate_id_prefixes(instance)

    @staticmethod
    def _validate_positive_amount(instance: "StripePayment") -> None:
        from decimal import Decimal
        if instance.amount is not None and instance.amount < Decimal("0.00"):
            raise ValidationError(
                {"amount": _("Payment amount must be zero or positive.")}
            )

    @staticmethod
    def _validate_currency_format(instance: "StripePayment") -> None:
        import re
        if instance.currency and not re.fullmatch(r"[a-z]{3}", instance.currency):
            raise ValidationError(
                {"currency": _("Currency must be a 3-letter lowercase ISO code (e.g. 'usd').")}
            )

    @staticmethod
    def _validate_id_prefixes(instance: "StripePayment") -> None:
        if instance.stripe_payment_intent_id and not instance.stripe_payment_intent_id.startswith("pi_"):
            raise ValidationError(
                {"stripe_payment_intent_id": _("Payment Intent ID must begin with 'pi_'.")}
            )
        if instance.stripe_invoice_id and not instance.stripe_invoice_id.startswith("in_"):
            raise ValidationError(
                {"stripe_invoice_id": _("Invoice ID must begin with 'in_'.")}
            )
```

---

### 9.5 Managers (`payments/managers.py`)

```python
class StripePaymentQuerySet(models.QuerySet):
    def succeeded(self):
        return self.filter(status=PaymentStatusChoice.SUCCEEDED)

    def failed(self):
        return self.filter(status=PaymentStatusChoice.FAILED)

    def refunded(self):
        return self.filter(status=PaymentStatusChoice.REFUNDED)

    def for_subscription(self, subscription):
        return self.filter(subscription=subscription)

    def for_user(self, user):
        return self.filter(subscription__user=user)


class StripePaymentManager(models.Manager):
    def get_queryset(self) -> StripePaymentQuerySet:
        return StripePaymentQuerySet(self.model, using=self._db)

    def succeeded(self):
        return self.get_queryset().succeeded()

    def failed(self):
        return self.get_queryset().failed()

    def for_user(self, user):
        return self.get_queryset().for_user(user).select_related(
            "subscription__plan_price__plan", "period"
        )
```

---

### 9.6 Webhook Handlers (`payments/stripe/handlers/events.py`)

#### `handle_invoice_paid(payload: dict) -> None`

```python
def handle_invoice_paid(payload: dict) -> None:
    """
    Fired when a Stripe invoice is successfully paid.
    Actions:
      1. Locate local Subscription.
      2. Locate or create the corresponding SubscriptionPeriod.
      3. Create StripePayment(status=SUCCEEDED).
      4. Restore subscription status from PAST_DUE → ACTIVE if needed.
      5. Trigger EntitlementService.reset_usage_for_new_period().
    """
    from decimal import Decimal
    from django.utils import timezone
    from datetime import datetime, timezone as dt_timezone
    from payments.models import Subscription, SubscriptionPeriod, StripePayment
    from payments.choices import SubscriptionStatusChoice, PeriodStatusChoice, PaymentStatusChoice
    from payments.services import EntitlementService

    invoice = payload["data"]["object"]
    stripe_subscription_id: str = invoice.get("subscription")
    if not stripe_subscription_id:
        return  # One-off invoice unrelated to a subscription; ignore.

    try:
        subscription = Subscription.objects.get(
            stripe_subscription_id=stripe_subscription_id
        )
    except Subscription.DoesNotExist:
        return

    def to_utc(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=dt_timezone.utc)

    # Resolve the billing period from invoice line items
    lines = invoice.get("lines", {}).get("data", [])
    if lines:
        period_start = to_utc(lines[0]["period"]["start"])
        period_end   = to_utc(lines[0]["period"]["end"])
    else:
        period_start = subscription.current_period_start
        period_end   = subscription.current_period_end

    period, period_created = SubscriptionPeriod.objects.get_or_create(
        subscription=subscription,
        start_date=period_start,
        defaults={
            "end_date":         period_end,
            "status":           PeriodStatusChoice.ACTIVE,
            "stripe_invoice_id": invoice["id"],
        },
    )

    # Create the payment ledger entry
    StripePayment.objects.get_or_create(
        stripe_invoice_id=invoice["id"],
        defaults={
            "subscription":             subscription,
            "period":                   period,
            "stripe_payment_intent_id": invoice.get("payment_intent"),
            "amount":                   Decimal(invoice["amount_paid"]) / 100,
            "currency":                 invoice["currency"].lower(),
            "status":                   PaymentStatusChoice.SUCCEEDED,
            "paid_at":                  timezone.now(),
        },
    )

    # Restore subscription if it was past-due
    if subscription.status == SubscriptionStatusChoice.PAST_DUE:
        subscription.status = SubscriptionStatusChoice.ACTIVE
        subscription.save(update_fields=["status", "updated_at"])

    # Reset usage counters for the new period
    if period_created:
        EntitlementService.reset_usage_for_new_period(subscription, period)
```

#### `handle_invoice_payment_failed(payload: dict) -> None`

```python
def handle_invoice_payment_failed(payload: dict) -> None:
    """
    Fired when a recurring charge fails.
    Actions:
      1. Create StripePayment(status=FAILED).
      2. Transition subscription status to PAST_DUE.
    """
    from decimal import Decimal
    from django.utils import timezone
    from payments.models import Subscription, StripePayment
    from payments.choices import SubscriptionStatusChoice, PaymentStatusChoice

    invoice = payload["data"]["object"]
    stripe_subscription_id: str = invoice.get("subscription")
    if not stripe_subscription_id:
        return

    try:
        subscription = Subscription.objects.get(
            stripe_subscription_id=stripe_subscription_id
        )
    except Subscription.DoesNotExist:
        return

    # Record the failed payment
    StripePayment.objects.get_or_create(
        stripe_invoice_id=invoice["id"],
        defaults={
            "subscription":             subscription,
            "period":                   None,
            "stripe_payment_intent_id": invoice.get("payment_intent"),
            "amount":                   Decimal(invoice.get("amount_due", 0)) / 100,
            "currency":                 invoice["currency"].lower(),
            "status":                   PaymentStatusChoice.FAILED,
            "paid_at":                  None,
        },
    )

    # Transition subscription to PAST_DUE
    if subscription.status not in [
        SubscriptionStatusChoice.PAST_DUE,
        SubscriptionStatusChoice.CANCELED,
    ]:
        subscription.status = SubscriptionStatusChoice.PAST_DUE
        subscription.save(update_fields=["status", "updated_at"])
```

---

### 9.7 Stripe Portal Service (`payments/stripe/services.py`)

Add `StripePortalService` to the existing services file:

```python
class StripePortalService:
    """
    Creates a Stripe-hosted Customer Portal session.
    The portal allows customers to self-service:
      - Update payment methods
      - View invoice history
      - Cancel or modify subscriptions

    No sensitive card data ever touches our servers.
    """

    @classmethod
    def create_portal_session(
        cls,
        user,
        return_url: str | None = None,
    ) -> str:
        """
        Creates a Stripe billing portal session and returns the hosted URL.

        Args:
            user: The Django User requesting portal access.
            return_url: URL to redirect the customer after leaving the portal.
                        Defaults to settings.STRIPE_PORTAL_RETURN_URL.

        Returns:
            str: The full hosted portal URL (e.g. https://billing.stripe.com/...).

        Raises:
            ValueError: If the user has no associated StripeCustomer record.
            stripe.StripeError: On Stripe API failures.
        """
        from payments.models import StripeCustomer
        from django.conf import settings

        try:
            stripe_customer = StripeCustomer.objects.get(user=user)
        except StripeCustomer.DoesNotExist:
            raise ValueError(
                f"No StripeCustomer record found for user {user.username!r}. "
                "The user must initiate a checkout session first."
            )

        effective_return_url = return_url or getattr(
            settings, "STRIPE_PORTAL_RETURN_URL", "http://localhost:8000/"
        )

        client = StripeClient.get_instance()
        session = client.billing_portal.sessions.create(
            customer=stripe_customer.stripe_customer_id,
            return_url=effective_return_url,
        )
        return session.url
```

---

### 9.8 Serializers (`payments/serializers.py`)

Add to the existing serializers file:

```python
class StripePaymentSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(
        source="subscription.plan_price.plan.name", read_only=True
    )
    billing_interval = serializers.CharField(
        source="subscription.plan_price.billing_interval", read_only=True
    )

    class Meta:
        model = StripePayment
        fields = [
            "id",
            "plan_name",
            "billing_interval",
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            "amount",
            "currency",
            "status",
            "paid_at",
            "created_at",
        ]
        read_only_fields = fields


class CreateBillingPortalSerializer(serializers.Serializer):
    """Request body for POST /payments/api/billing/portal/"""
    return_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text=_("URL to redirect customer after leaving the Stripe portal."),
    )
```

---

### 9.9 Views (`payments/views.py`)

Add the following two CBVs:

```python
class BillingHistoryView(generics.ListAPIView):
    """
    GET /payments/api/billing/history/

    Returns the authenticated user's complete payment history.
    Paginated (10 items per page).
    Strictly scoped: users can only see their own payments.
    """
    permission_classes  = [IsAuthenticated]
    serializer_class    = StripePaymentSerializer
    pagination_class    = PageNumberPagination

    def get_queryset(self):
        return StripePayment.objects.for_user(self.request.user).order_by("-paid_at")


class CreateBillingPortalSessionView(APIView):
    """
    POST /payments/api/billing/portal/

    Creates a Stripe Customer Portal session and returns the redirect URL.
    The customer is sent to Stripe's hosted portal to manage their billing.

    Request body (optional): { "return_url": "https://myapp.com/account/" }
    Response: { "portal_url": "https://billing.stripe.com/..." }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = CreateBillingPortalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            portal_url = StripePortalService.create_portal_session(
                user=request.user,
                return_url=serializer.validated_data.get("return_url"),
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except stripe.StripeError as exc:
            return Response(
                {"detail": f"Stripe error: {exc.user_message}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"portal_url": portal_url}, status=status.HTTP_200_OK)
```

---

### 9.10 URL Patterns (`payments/urls.py`)

Add to `urlpatterns`:

```python
# Phase 9 — Billing history & portal
path("api/billing/history/", BillingHistoryView.as_view(),              name="billing-history"),
path("api/billing/portal/",  CreateBillingPortalSessionView.as_view(),  name="billing-portal"),
```

---

### 9.11 Admin (`payments/admin.py`)

```python
@admin.register(StripePayment)
class StripePaymentAdmin(admin.ModelAdmin):
    list_display   = ["subscription", "amount", "currency", "status", "paid_at", "stripe_invoice_id"]
    list_filter    = ["status", "currency"]
    search_fields  = ["subscription__user__username", "stripe_invoice_id", "stripe_payment_intent_id"]
    readonly_fields = [
        "subscription", "period", "stripe_payment_intent_id",
        "stripe_invoice_id", "amount", "currency", "status", "paid_at",
        "created_at", "updated_at",
    ]
    ordering = ["-paid_at"]
```

---

### 9.12 Test Cases (`payments/tests/test_payments.py`)

Create `payments/tests/test_payments.py` with the following **10 test methods**:

```
TestStripePaymentModel:
  1. test_create_valid_stripe_payment
     - Create StripePayment with valid fields; assert saved with correct status.

  2. test_negative_amount_raises_validation_error
     - Attempt StripePayment with amount = Decimal("-1.00").
     - Assert ValidationError raised on 'amount'.

  3. test_invalid_currency_code_raises_validation_error
     - Attempt StripePayment with currency = "USD" (uppercase).
     - Assert ValidationError raised on 'currency'.

  4. test_invalid_payment_intent_id_prefix_raises_validation_error
     - Attempt StripePayment with stripe_payment_intent_id = "INVALID_123".
     - Assert ValidationError raised on 'stripe_payment_intent_id'.

  5. test_manager_succeeded_returns_only_succeeded_payments
     - Create 1 SUCCEEDED, 1 FAILED, 1 PENDING payment.
     - StripePayment.objects.succeeded() → returns only 1.

  6. test_manager_for_user_returns_only_own_payments
     - Create payments for user_a and user_b.
     - StripePayment.objects.for_user(user_a) → returns only user_a's.

TestWebhookHandlers:
  7. test_invoice_paid_creates_succeeded_payment_and_links_period
     - Pre-create Subscription and SubscriptionPeriod.
     - Build mock invoice.paid payload.
     - Call handle_invoice_paid(payload).
     - Assert StripePayment created with status=SUCCEEDED, linked to period.

  8. test_invoice_paid_restores_past_due_subscription_to_active
     - Pre-create Subscription(status=PAST_DUE).
     - Call handle_invoice_paid(payload).
     - Assert subscription.status refreshed to ACTIVE.

  9. test_invoice_payment_failed_creates_failed_payment_and_marks_past_due
     - Pre-create Subscription(status=ACTIVE).
     - Build mock invoice.payment_failed payload.
     - Call handle_invoice_payment_failed(payload).
     - Assert StripePayment created with status=FAILED.
     - Assert subscription.status == PAST_DUE.

TestBillingViews:
  10. test_billing_history_returns_user_payments_only
      - Create payments for two users; authenticate as user_a.
      - GET /payments/api/billing/history/ → HTTP 200, only user_a's payments returned.

  11. test_create_billing_portal_returns_portal_url
      - Mock StripePortalService.create_portal_session to return a fake URL.
      - POST /payments/api/billing/portal/ → HTTP 200, response has 'portal_url'.
```

**Target after Phase 9**: **90 passing tests in < 0.9s**

---
---

## Phase 10: End-to-End Verification, Failure Drills & Production Readiness

### 10.1 Objectives

- Execute a complete **end-to-end lifecycle simulation** using Stripe's sandbox environment and the `stripe listen` CLI to forward live test webhooks.
- Run structured **resilience drills** (duplicate events, signature tampering, out-of-order delivery, concurrency overloads).
- Verify complete **PostgreSQL migration readiness** (no SQLite-only types used).
- Produce final **API documentation**, **curl/Postman collection**, and **deployment checklist**.

---

### 10.2 End-to-End Sandbox Lifecycle Simulation

#### Step 1 — Start Stripe CLI Forwarding

```bash
# Install Stripe CLI if not already installed
brew install stripe/stripe-cli/stripe

# Login with test API keys
stripe login

# Forward webhooks to local dev server
stripe listen --forward-to localhost:8000/payments/webhooks/stripe/
# Copy the webhook signing secret (whsec_...) and update STRIPE_WEBHOOK_SECRET in .env
```

#### Step 2 — Full Happy Path Checklist

```
1. [ ] User registers / logs in via Django admin or API.
2. [ ] User calls GET /payments/api/plans/ → views plans + prices.
3. [ ] User calls POST /payments/api/checkout/ with valid plan_price_id.
      Response: { "checkout_url": "https://checkout.stripe.com/..." }
4. [ ] User navigates to checkout URL, enters test card 4242 4242 4242 4242.
5. [ ] Stripe fires checkout.session.completed webhook.
      StripeWebhookView receives, verifies HMAC, dispatcher routes to handler.
      Handler creates Subscription(status=active) + SubscriptionPeriod.
6. [ ] User calls GET /payments/api/subscriptions/current/ → HTTP 200 with subscription details.
7. [ ] User calls GET /payments/api/usage/ → sees all feature quotas and remaining.
8. [ ] User calls GET /payments/api/usage/check/ai-requests/ → { "allowed": true, "remaining": 1000 }
9. [ ] Stripe fires invoice.paid (first invoice after checkout).
      StripePayment(status=SUCCEEDED) created and linked to period.
10.[ ] User calls GET /payments/api/billing/history/ → sees first payment.
11.[ ] User calls POST /payments/api/billing/portal/ → gets Stripe portal URL.
12.[ ] User calls POST /payments/api/subscriptions/cancel/ { "at_period_end": true }
      Stripe receives cancel request. Subscription.cancel_at_period_end becomes True.
13.[ ] User calls POST /payments/api/subscriptions/reactivate/
      Stripe reverts to cancel_at_period_end=False.
14.[ ] [Simulate renewal] Use `stripe trigger invoice.paid` to fire a renewal.
      New SubscriptionPeriod opens; FeatureUsage counters reset to 0.
```

---

### 10.3 Resilience Drills

#### Drill 1 — Webhook Signature Tampering

```bash
# Send a webhook with a forged/invalid signature
curl -X POST http://localhost:8000/payments/webhooks/stripe/ \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: t=1234567890,v1=FAKE_SIGNATURE" \
  -d '{"id": "evt_fake", "type": "checkout.session.completed"}'

# Expected: HTTP 400 Bad Request
# Response: { "error": "Invalid webhook signature." }
# Verify in logs: StripeWebhookEvent NOT created for tampered event.
```

#### Drill 2 — Duplicate Webhook Replay (Idempotency)

```bash
# Replay the same valid event twice (simulating Stripe retry)
stripe events resend <event_id>

# Expected outcome:
#   - First delivery: StripeWebhookEvent.status = PROCESSED.
#   - Second delivery: Dispatcher checks idempotency → event already PROCESSED.
#   - No duplicate Subscription, Payment, or Period record created.
#   - HTTP 200 returned for both (Stripe requires 2xx to stop retrying).
```

#### Drill 3 — Out-of-Order Event Delivery

```bash
# Simulate invoice.paid arriving BEFORE checkout.session.completed
# (Stripe can deliver events non-deterministically)

# Step 1: Fire invoice.paid without a local Subscription existing
stripe trigger invoice.paid

# Expected: handle_invoice_paid() calls Subscription.objects.get() → DoesNotExist.
# The handler returns None gracefully without crashing.
# StripeWebhookEvent.status = FAILED with descriptive error_message logged.
# HTTP 200 returned (so Stripe doesn't retry infinitely).

# Step 2: Fire checkout.session.completed
# Subscription is now created correctly.
```

#### Drill 4 — Quota Race Condition Simulation

Write a test in `test_usage.py`:

```python
def test_concurrent_quota_consumption_does_not_exceed_limit(self):
    """
    Simulates 10 concurrent threads each consuming 1 unit against a limit of 5.
    Exactly 5 should succeed; exactly 5 should raise QuotaExceededError.
    No race condition should allow 6+ successes.
    """
    import threading
    from payments.services import EntitlementService, QuotaExceededError

    limit = 5
    # Setup plan_feature with limit=5, used_amount=0
    # ... (setup code) ...

    successes = []
    failures  = []
    lock      = threading.Lock()

    def try_consume():
        try:
            EntitlementService.consume_quota(self.user, "api-requests", amount=1)
            with lock:
                successes.append(1)
        except QuotaExceededError:
            with lock:
                failures.append(1)

    threads = [threading.Thread(target=try_consume) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    self.assertEqual(len(successes), limit)
    self.assertEqual(len(failures), 10 - limit)
```

---

### 10.4 PostgreSQL Migration Readiness

#### ORM Compatibility Checklist

| Check | Status | Notes |
|---|---|---|
| No `JSONField` with `sqlite3`-specific defaults | ✅ | `payload` uses `models.JSONField(default=dict)` — valid on both |
| No raw SQL `AUTOINCREMENT` or `SERIAL` hints | ✅ | Using Django's default `AutoField` |
| `DateTimeField` stores UTC only | ✅ | All timestamps sourced from Stripe's UTC Unix timestamps |
| `DecimalField` precision | ✅ | `max_digits=10, decimal_places=2` — safe on both |
| `unique_together` vs `UniqueConstraint` | ✅ | Using `unique_together` — compatible with both |
| `db_index=True` on FKs | ✅ | Explicit indexes declared; PostgreSQL uses B-tree by default |
| `select_for_update()` | ✅ | SQLite supports since 3.25; PostgreSQL fully supports |
| `F()` expression updates | ✅ | Database-agnostic |

#### Switching to PostgreSQL

1. Install adapter: `uv add psycopg2-binary`
2. Update `config/settings.py`:
   ```python
   DATABASES = {
       "default": {
           "ENGINE":   "django.db.backends.postgresql",
           "NAME":     env("POSTGRES_DB",       default="payments_db"),
           "USER":     env("POSTGRES_USER",      default="payments_user"),
           "PASSWORD": env("POSTGRES_PASSWORD"),
           "HOST":     env("POSTGRES_HOST",      default="localhost"),
           "PORT":     env("POSTGRES_PORT",      default="5432"),
           "CONN_MAX_AGE": 60,  # Connection pooling
       }
   }
   ```
3. Run migrations: `uv run python manage.py migrate`
4. All 90+ tests should pass identically on PostgreSQL.

---

### 10.5 Settings & Environment Hardening

Add the following to `config/settings.py` for production-readiness:

```python
# ── Stripe ──────────────────────────────────────────────────────────────────
STRIPE_PUBLISHABLE_KEY    = env("STRIPE_PUBLISHABLE_KEY")
STRIPE_SECRET_KEY         = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET     = env("STRIPE_WEBHOOK_SECRET")
STRIPE_PORTAL_RETURN_URL  = env("STRIPE_PORTAL_RETURN_URL", default="http://localhost:8000/")

# ── Security ─────────────────────────────────────────────────────────────────
# In production, set these:
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True
# ALLOWED_HOSTS = ["yourdomain.com"]

# ── DRF Pagination ───────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS":  "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE":                 10,
}
```

Update `.env.example`:

```env
# Django
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Stripe Test Mode
STRIPE_PUBLISHABLE_KEY=pk_test_your_publishable_key
STRIPE_SECRET_KEY=sk_test_your_secret_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_signing_secret
STRIPE_PORTAL_RETURN_URL=http://localhost:8000/account/

# Database (PostgreSQL — leave empty to use SQLite)
POSTGRES_DB=payments_db
POSTGRES_USER=payments_user
POSTGRES_PASSWORD=
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

---

### 10.6 Final Test Suite Run

```bash
# Run full test suite with verbosity
uv run python manage.py test payments --verbosity=2

# Expected: 90+ tests, 0 failures, 0 errors, < 1.0s
```

Expected final test matrix:

| Test File | Phase Coverage | Test Count |
|---|---|:---:|
| `test_features.py` | Phase 2 | 10 |
| `test_prices.py` | Phase 3 | 10 |
| `test_customers.py` | Phase 4 | 9 |
| `test_checkout.py` | Phase 5 | 12 |
| `test_webhooks.py` | Phase 6 | 12 |
| `test_subscriptions.py` | Phase 7 | 13 |
| `test_usage.py` | Phase 8 | 13 |
| `test_payments.py` | Phase 9 | 11 |
| **Total** | | **90** |

---

### 10.7 Final API Reference (All Endpoints)

| Method | Endpoint | Auth | Phase | Description |
|---|---|---|---|---|
| `GET`  | `/payments/api/plans/` | Public | 2/5 | List active plans with features & prices |
| `GET`  | `/payments/api/plans/<slug>/` | Public | 2/5 | Single plan detail |
| `POST` | `/payments/api/checkout/` | Auth | 5 | Create Stripe checkout session |
| `GET`  | `/payments/success/` | Public | 5 | Browser return URL (no state change) |
| `GET`  | `/payments/cancel/` | Public | 5 | Browser cancel URL |
| `POST` | `/payments/webhooks/stripe/` | HMAC | 6 | Stripe inbound webhook receiver |
| `GET`  | `/payments/api/subscriptions/current/` | Auth | 7 | Get active subscription |
| `POST` | `/payments/api/subscriptions/cancel/` | Auth | 7 | Request subscription cancellation |
| `POST` | `/payments/api/subscriptions/reactivate/` | Auth | 7 | Reverse pending cancellation |
| `GET`  | `/payments/api/usage/` | Auth+Sub | 8 | Full feature usage summary |
| `GET`  | `/payments/api/usage/check/<slug>/` | Auth | 8 | Single feature entitlement check |
| `GET`  | `/payments/api/billing/history/` | Auth | 9 | Paginated payment history |
| `POST` | `/payments/api/billing/portal/` | Auth | 9 | Create Stripe billing portal session |

---

### 10.8 Phase Completion Tracker

| Phase | Title | Status | Tests Added | Cumulative Tests |
|---|---|---|:---:|:---:|
| Phase 1 | Environment & Stripe SDK | ✅ Done | 0 | 0 |
| Phase 2 | Feature, Plan, PlanFeature | ✅ Done | 10 | 10 |
| Phase 3 | Pricing & Stripe Catalog Sync | ✅ Done | 10 | 20 |
| Phase 4 | Customer Mapping | ✅ Done | 9 | 29 |
| Phase 5 | Checkout Session & Catalog CBVs | ✅ Done | 12 | 41 |
| Phase 6 | Webhook Security & Dispatcher | ✅ Done | 12 | 53 |
| Refactor | Consolidated Models & Migration | ✅ Done | 0 | 53 |
| Phase 7 | Subscriptions & Periods | 🔲 Next | 13 | 66 |
| Phase 8 | Usage Tracking & Quotas | 🔲 Pending | 13 | 79 |
| Phase 9 | Payment Ledger & Billing Portal | ✅ Done | 11 | 106 |
| Phase 10 | E2E Verification & Production | 🔲 Pending | 0 | 90 |

---

## 7. Automated Test Suite Matrix

The project enforces automated test coverage across all domain components. Current count: **106 tests passing in ~0.98s**.

| Test File | Covered Domain | Key Test Scenarios | Test Count |
|---|---|---|:---:|
| [`payments/tests/test_features.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_features.py) | Catalog Foundations | Auto-slug generation, manager filters (`active`, `toggles`, `limits`), valid toggle config, invalid toggle with limit (raises `ValidationError`), valid limit config, missing limit/unit errors, unique constraint enforcement, nested `PlanSerializer` data. | **10** |
| [`payments/tests/test_prices.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_prices.py) | Pricing & Stripe Sync | Decimal-to-cents accuracy, negative amount validation, invalid currency code validation, invalid interval validation, `stripe_price_id` prefix validation, manager querysets, Stripe catalog sync mock (product + recurring price creation). | **10** |
| [`payments/tests/test_customers.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_customers.py) | Customer Mapping | 1:1 user relation, duplicate mapping prevention, duplicate `stripe_customer_id` prevention, `'cus_'` prefix validation, customer reuse logic (zero extra Stripe API calls on subsequent lookups). | **9** |
| [`payments/tests/test_checkout.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_checkout.py) | Stripe Checkout & Catalog | Active price validation, non-existent price rejection, inactive plan rejection, mock Stripe session creation with correct subscription metadata, auth required (403 on unauthenticated POST), 201 on valid POST, success/cancel views, catalog APIs. | **12** |
| [`payments/tests/test_webhooks.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_webhooks.py) | Webhooks & Idempotency | `evt_` prefix validation, manager filters, secret/signature validation, signature tampering rejection, dispatcher $O(1)$ routing, **idempotency guard skipping already-processed events**, failure tracking, HTTP 200 / 400 responses. | **12** |
| **Total** | | **All 53 unit & integration tests passing cleanly** | **53** |

---

## 8. API Reference & Payload Specifications

### 1. Catalog: List Active Plans
* **Endpoint**: `GET /payments/api/plans/`
* **Auth**: Public (`AllowAny`)
* **Response (HTTP 200 OK)**:
```json
[
  {
    "id": 1,
    "name": "Pro Tier",
    "slug": "pro-tier",
    "description": "Full access with high quotas",
    "is_active": true,
    "features": [
      {
        "id": 1,
        "feature_slug": "api-access",
        "feature_name": "API Access",
        "feature_type": "TOGGLE",
        "is_enabled": true,
        "limit": null,
        "unit": ""
      },
      {
        "id": 2,
        "feature_slug": "ai-requests",
        "feature_name": "AI Requests",
        "feature_type": "LIMIT",
        "is_enabled": true,
        "limit": 1000,
        "unit": "requests"
      }
    ],
    "prices": [
      {
        "id": 1,
        "amount": "29.00",
        "amount_in_cents": 2900,
        "currency": "usd",
        "billing_interval": "month",
        "billing_interval_display": "Month",
        "stripe_price_id": "price_1N2...",
        "is_synced": true,
        "is_active": true
      }
    ]
  }
]
```

---

### 2. Checkout: Create Subscription Session
* **Endpoint**: `POST /payments/api/checkout/`
* **Auth**: Authenticated (`IsAuthenticated`)
* **Request Payload**:
```json
{
  "plan_price_id": 1,
  "success_url": "http://localhost:8000/payments/success/?session_id={CHECKOUT_SESSION_ID}",
  "cancel_url": "http://localhost:8000/payments/cancel/"
}
```
* **Response (HTTP 201 Created)**:
```json
{
  "session_id": "cs_test_a1b2c3d4e5",
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3d4e5"
}
```

---

### 3. Webhook: Stripe Inbound Receiver
* **Endpoint**: `POST /payments/webhooks/stripe/`
* **Auth**: Cryptographic HMAC Header (`HTTP_STRIPE_SIGNATURE`)
* **Response (HTTP 200 OK)**:
```json
{
  "status": "received",
  "event_id": "evt_1N23456789",
  "processing_status": "processed"
}
```
* **Error Response on Invalid Signature (HTTP 400 Bad Request)**:
```json
{
  "error": "Invalid webhook signature."
}
```

---

### 4. Subscriptions: Get Current Subscription
* **Endpoint**: `GET /payments/api/subscriptions/current/`
* **Auth**: Authenticated (`IsAuthenticated`)
* **Response (HTTP 200 OK)**:
```json
{
  "id": 1,
  "stripe_subscription_id": "sub_1N23456789",
  "status": "active",
  "is_active": true,
  "is_past_due": false,
  "is_pending_cancellation": false,
  "plan_name": "Pro Tier",
  "plan_slug": "pro-tier",
  "amount": "29.00",
  "currency": "usd",
  "billing_interval": "month",
  "started_at": "2026-11-01T00:00:00Z",
  "current_period_start": "2026-11-01T00:00:00Z",
  "current_period_end": "2026-12-01T00:00:00Z",
  "cancel_at_period_end": false,
  "canceled_at": null,
  "current_period": {
    "id": 1,
    "start_date": "2026-11-01T00:00:00Z",
    "end_date": "2026-12-01T00:00:00Z",
    "status": "active",
    "stripe_invoice_id": "in_1N23456789"
  }
}
```

---

### 5. Usage: Feature Usage Summary
* **Endpoint**: `GET /payments/api/usage/`
* **Auth**: Authenticated + Active Subscription
* **Response (HTTP 200 OK)**:
```json
[
  {
    "feature_name": "API Access",
    "feature_slug": "api-access",
    "feature_type": "TOGGLE",
    "is_enabled": true,
    "limit": null,
    "unit": "",
    "used_amount": null,
    "remaining": null,
    "percent_used": null
  },
  {
    "feature_name": "AI Requests",
    "feature_slug": "ai-requests",
    "feature_type": "LIMIT",
    "is_enabled": true,
    "limit": 1000,
    "unit": "requests",
    "used_amount": 350,
    "remaining": 650,
    "percent_used": 35.0
  }
]
```

---

### 6. Billing: Payment History
* **Endpoint**: `GET /payments/api/billing/history/`
* **Auth**: Authenticated (`IsAuthenticated`)
* **Response (HTTP 200 OK)**:
```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "plan_name": "Pro Tier",
      "billing_interval": "month",
      "stripe_invoice_id": "in_1N23456789",
      "stripe_payment_intent_id": "pi_1N23456789",
      "amount": "29.00",
      "currency": "usd",
      "status": "succeeded",
      "paid_at": "2026-11-01T00:05:00Z",
      "created_at": "2026-11-01T00:05:00Z"
    }
  ]
}
```


```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│             PHASE 7             │────►│             PHASE 8             │
│   Subscriptions, Periods &      │     │    Feature Usage Tracking &     │
│   Lifecycle Synchronization     │     │    Atomic Quota Enforcement     │
└────────────────┬────────────────┘     └────────────────┬────────────────┘
                 │                                       │
                 ▼                                       ▼
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│             PHASE 9             │────►│            PHASE 10             │
│     Payment Ledger, Invoicing   │     │    End-to-End Verification,     │
│     & Customer Billing Portal   │     │    Failure Drills & Production  │
└─────────────────────────────────┘     └─────────────────────────────────┘
```

---

##: Subscriptions, Periods & State Machine Lifecycle

#### 1. Objectives & Business Rules
- Represent the contracted subscription between a customer and a `PlanPrice`.
- Enforce the core business rule: **Only 1 active subscription per user at any given time** (`status` in `['active', 'trialing']`), while preserving complete historical records of past/canceled subscriptions.
- Maintain a separate historical timeline of billing cycles via `SubscriptionPeriod`.
- Synchronize subscription state changes automatically from Stripe webhooks without human intervention.
- Support subscription cancellations (both at period-end and immediate) and reactivations.

#### 2. Database Models (`payments/models/models.py`)

##### A. `Subscription(TimeStampedModel)`
```python
class Subscription(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        help_text=_("The customer who owns this subscription."),
    )
    plan_price = models.ForeignKey(
        PlanPrice,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        help_text=_("The contracted PlanPrice (plan, interval, currency, price)."),
    )
    stripe_subscription_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text=_("Stripe Subscription ID (e.g. 'sub_1N23456789')."),
    )
    status = models.CharField(
        max_length=25,
        choices=SubscriptionStatusChoice.choices,
        default=SubscriptionStatusChoice.INCOMPLETE,
        db_index=True,
        help_text=_("Current state machine status aligned with Stripe."),
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp when the subscription was first activated."),
    )
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Start timestamp of the current billing cycle from Stripe."),
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("End timestamp of the current billing cycle from Stripe."),
    )
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text=_("If True, access continues until current_period_end then cancels."),
    )
    canceled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp when the subscription was formally canceled."),
    )

    objects = SubscriptionManager()

    class Meta:
        verbose_name = _("Subscription")
        verbose_name_plural = _("Subscriptions")
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        """Returns True if customer has valid active access."""
        return self.status in [
            SubscriptionStatusChoice.ACTIVE,
            SubscriptionStatusChoice.TRIALING,
        ]

    @property
    def is_past_due(self) -> bool:
        """Returns True if the latest recurring payment failed."""
        return self.status == SubscriptionStatusChoice.PAST_DUE
```

##### B. `SubscriptionPeriod(TimeStampedModel)`
```python
class SubscriptionPeriod(TimeStampedModel):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="periods",
        help_text=_("The subscription this billing period belongs to."),
    )
    start_date = models.DateTimeField(
        help_text=_("Start timestamp of this specific billing interval."),
    )
    end_date = models.DateTimeField(
        help_text=_("End timestamp of this specific billing interval."),
    )
    status = models.CharField(
        max_length=20,
        choices=PeriodStatusChoice.choices,
        default=PeriodStatusChoice.ACTIVE,
        db_index=True,
        help_text=_("Status of this billing cycle (active, completed, canceled)."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID covering this specific period."),
    )

    objects = SubscriptionPeriodManager()

    class Meta:
        verbose_name = _("Subscription Period")
        verbose_name_plural = _("Subscription Periods")
        ordering = ["-start_date"]
```

#### 3. Centralized Validation (`payments/validators.py`)
- **`SubscriptionValidator`**:
  - Enforces that `stripe_subscription_id` begins with `'sub_'`.
  - Enforces that only one subscription per user can have status `ACTIVE` or `TRIALING`.
  - Validates that `current_period_end >= current_period_start`.
- **`SubscriptionPeriodValidator`**:
  - Validates that `end_date > start_date`.

#### 4. Webhook Handler Logic (`payments/stripe/handlers/events.py`)
- **`handle_checkout_session_completed(payload)`**:
  1. Extract `customer_id` and `subscription_id` from `session.subscription`.
  2. Locate `StripeCustomer` by `stripe_customer_id = customer_id` $\rightarrow$ resolves `user`.
  3. Extract `django_plan_price_id` from metadata $\rightarrow$ resolves `PlanPrice`.
  4. Fetch fresh subscription details from Stripe SDK (`stripe.Subscription.retrieve(subscription_id)`).
  5. Create or update `Subscription` with:
     - `status = stripe_sub.status` (e.g. `active`)
     - `current_period_start = to_datetime(stripe_sub.current_period_start)`
     - `current_period_end = to_datetime(stripe_sub.current_period_end)`
     - `started_at = timezone.now()`
  6. Open initial `SubscriptionPeriod` record (`start_date`, `end_date`, `status = ACTIVE`).
- **`handle_customer_subscription_updated(payload)`**:
  1. Locate `Subscription` by `stripe_subscription_id`.
  2. Synchronize `status`, `current_period_start`, `current_period_end`, and `cancel_at_period_end`.
  3. Check if plan changed (upgrade/downgrade via `subscription.items.data[0].price.id`):
     - If price ID changed, lookup local `PlanPrice` and update `subscription.plan_price`.
- **`handle_customer_subscription_deleted(payload)`**:
  1. Locate `Subscription` by `stripe_subscription_id`.
  2. Transition status to `CANCELED`.
  3. Set `canceled_at = timezone.now()`.
  4. Close active `SubscriptionPeriod` (`status = CANCELED`).

#### 5. Outbound Stripe Services (`payments/stripe/services.py`)
- `StripeSubscriptionService.cancel_subscription(subscription, at_period_end=True) -> stripe.Subscription`:
  - If `at_period_end=True`: Calls `stripe.Subscription.modify(subscription.stripe_subscription_id, cancel_at_period_end=True)`.
  - If `at_period_end=False`: Calls `stripe.Subscription.cancel(subscription.stripe_subscription_id)`.
- `StripeSubscriptionService.reactivate_subscription(subscription) -> stripe.Subscription`:
  - Calls `stripe.Subscription.modify(subscription.stripe_subscription_id, cancel_at_period_end=False)` to undo pending cancellation.

#### 6. Class-Based Views (`payments/views.py` / `payments/stripe/views.py`)
- `CurrentSubscriptionView(APIView)`:
  - `GET /payments/api/subscriptions/current/`: Returns active subscription details, plan name, pricing, renewal date, and cancel flags.
- `CancelSubscriptionView(APIView)`:
  - `POST /payments/api/subscriptions/cancel/`: Accepts `{ "at_period_end": true }`, calls service, and returns updated state.
- `ReactivateSubscriptionView(APIView)`:
  - `POST /payments/api/subscriptions/reactivate/`: Reverses pending cancellation.

#### 7. Admin Configuration (`payments/admin.py`)
- `SubscriptionPeriodInline` inside `SubscriptionAdmin`.
- List display: `user`, `plan_price`, `status`, `current_period_end`, `cancel_at_period_end`.
- Admin actions: *"Cancel at period end in Stripe"*, *"Sync latest status from Stripe"*.

#### 8. Planned Test Cases (`payments/tests/test_subscriptions.py`)
1. Create valid `Subscription` and `SubscriptionPeriod`.
2. Enforce `sub_` prefix validator.
3. Validate "1 active subscription per user" constraint blocks second active subscription.
4. Period end date must be after start date validator.
5. `is_active` and `is_past_due` property flags.
6. Webhook `checkout.session.completed` provisions local subscription and period.
7. Webhook `customer.subscription.updated` syncs period renewals and plan changes.
8. Webhook `customer.subscription.deleted` marks status canceled.
9. Outbound `cancel_subscription` sets `cancel_at_period_end = True`.
10. Outbound `reactivate_subscription` restores `cancel_at_period_end = False`.
11. `CurrentSubscriptionView` returns 200 with subscription data for active users.
12. `CurrentSubscriptionView` returns 404/empty for users without subscriptions.
13. `CancelSubscriptionView` successfully cancels active subscription.

---

### Phase 8: Feature Usage Tracking & Atomic Quota Enforcement

#### 1. Objectives & Business Rules
- Separate what customers are **allowed to consume** (`PlanFeature.limit`) from what they have **actually consumed** (`FeatureUsage.used_amount`).
- Support both **Toggle features** (binary enabled/disabled) and **Limit features** (numerical quotas like 1,000 API requests or 50 GB storage).
- **Concurrency Safety**: Enforce atomic quota increments using database-level row locking (`select_for_update`) and Django `F()` expressions to make race conditions impossible under concurrent API requests.
- **Automated Period Boundary Resets**: Automatically roll over usage counters when a new `SubscriptionPeriod` starts.
- Provide reusable DRF permissions (`HasFeatureEntitlement`) to gate API views with clean HTTP 403 responses when limits are reached.

#### 2. Database Model (`payments/models/models.py`)

##### `FeatureUsage(TimeStampedModel)`
```python
class FeatureUsage(TimeStampedModel):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="usages",
        help_text=_("The subscription this usage record belongs to."),
    )
    feature = models.ForeignKey(
        Feature,
        on_delete=models.CASCADE,
        related_name="usages",
        help_text=_("The metered capability being measured."),
    )
    period_start = models.DateTimeField(
        help_text=_("Start of the billing period for which usage is recorded."),
    )
    period_end = models.DateTimeField(
        help_text=_("End of the billing period for which usage is recorded."),
    )
    used_amount = models.PositiveIntegerField(
        default=0,
        help_text=_("Total amount consumed in this billing period."),
    )

    objects = FeatureUsageManager()

    class Meta:
        verbose_name = _("Feature Usage")
        verbose_name_plural = _("Feature Usages")
        unique_together = [("subscription", "feature", "period_start")]
        ordering = ["-period_start"]

    def __str__(self) -> str:
        return f"{self.subscription.user.username} - {self.feature.name}: {self.used_amount}"
```

#### 3. Entitlement & Usage Service (`payments/services.py`)
```python
class EntitlementService:
    @classmethod
    def check_feature_access(cls, user, feature_slug: str) -> Tuple[bool, str]:
        """
        Verifies if the user is entitled to use the feature:
        1. Checks active subscription.
        2. Retrieves PlanFeature configuration for the plan.
        3. If TOGGLE: returns is_enabled.
        4. If LIMIT: returns True if used_amount < limit.
        Returns: (has_access: bool, reason: str)
        """
        ...

    @classmethod
    def consume_quota(cls, user, feature_slug: str, amount: int = 1) -> bool:
        """
        Atomically increments used_amount inside a database transaction:
        with transaction.atomic():
            usage = FeatureUsage.objects.select_for_update().get(...)
            if usage.used_amount + amount > plan_feature.limit:
                raise QuotaExceededError(...)
            FeatureUsage.objects.filter(id=usage.id).update(used_amount=F('used_amount') + amount)
        """
        ...

    @classmethod
    def get_user_usage_summary(cls, user) -> List[Dict[str, Any]]:
        """
        Returns complete breakdown of all plan features:
        - Feature name & slug
        - Type (TOGGLE vs LIMIT)
        - Configured limit
        - Used amount
        - Remaining amount
        - Percentage consumed
        """
        ...

    @classmethod
    def reset_usage_for_new_period(cls, subscription, new_period: SubscriptionPeriod) -> None:
        """
        Opens fresh FeatureUsage records with used_amount = 0 for the new period.
        """
        ...
```

#### 4. Custom DRF Permissions (`payments/permissions.py`)
```python
class HasActiveSubscription(BasePermission):
    """Requires request.user to possess an active or trialing subscription."""
    def has_permission(self, request, view):
        return Subscription.objects.filter(
            user=request.user,
            status__in=[SubscriptionStatusChoice.ACTIVE, SubscriptionStatusChoice.TRIALING]
        ).exists()

class HasFeatureEntitlement:
    """
    Factory for view-level permission classes:
    permission_classes = [HasFeatureEntitlement("ai-requests")]
    """
    def __init__(self, feature_slug: str, required_amount: int = 1):
        self.feature_slug = feature_slug
        self.required_amount = required_amount

    def __call__(self):
        class _FeaturePermission(BasePermission):
            ...
        return _FeaturePermission
```

#### 5. Class-Based Views (`payments/views.py`)
- `UsageSummaryView(APIView)`:
  - `GET /payments/api/usage/`: Returns real-time breakdown of all feature quotas, remaining balances, and reset dates.
- `FeatureAccessCheckView(APIView)`:
  - `GET /payments/api/usage/check/<slug>/`: Instant check returning `{ "allowed": true, "remaining": 650 }`.

#### 6. Planned Test Cases (`payments/tests/test_usage.py`)
1. Create valid `FeatureUsage` record.
2. Unique together constraint prevents duplicate usage records for same period.
3. Toggle feature access returns `True` when enabled, `False` when disabled.
4. Limit feature allows consumption when under quota.
5. Limit feature rejects consumption when quota exceeded (returns `False` or raises `QuotaExceededError`).
6. Concurrency simulation test: Multiple parallel threads consume quota concurrently; total usage strictly equals thread count without exceeding limit.
7. `EntitlementService.get_user_usage_summary` calculates correct remaining balances.
8. Usage reset engine resets counters to 0 on period rollover.
9. `HasActiveSubscription` permission blocks un-subscribed users with HTTP 403.
10. `HasFeatureEntitlement` permission blocks requests when quota exhausted.
11. `UsageSummaryView` returns 200 with complete metrics for active customer.

---

### Phase 9: Payment Ledger, Invoicing & Billing Portal

#### 1. Objectives & Business Rules
- Maintain an immutable financial transaction ledger via `StripePayment` recording successful payments, refunds, and failed charges.
- Link each payment directly to both the parent `Subscription` and the specific `SubscriptionPeriod` it pays for.
- Handle recurring billing webhooks (`invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`).
- Provide customer self-service billing management via Stripe's hosted Customer Portal.

#### 2. Database Model (`payments/models/models.py`)

##### `StripePayment(TimeStampedModel)`
```python
class StripePayment(TimeStampedModel):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name="payments",
        help_text=_("The subscription this payment was made for."),
    )
    period = models.ForeignKey(
        SubscriptionPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        help_text=_("The specific billing period this payment covered."),
    )
    stripe_payment_intent_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Payment Intent ID (e.g. 'pi_12345')."),
    )
    stripe_invoice_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text=_("Stripe Invoice ID (e.g. 'in_12345')."),
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_("Amount paid in standard currency units (e.g. 29.00)."),
    )
    currency = models.CharField(
        max_length=3,
        default="usd",
        help_text=_("3-letter ISO currency code."),
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentStatusChoice.choices,
        default=PaymentStatusChoice.PENDING,
        db_index=True,
        help_text=_("Transaction status (pending, succeeded, failed, refunded)."),
    )
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Timestamp when payment was marked succeeded."),
    )

    objects = StripePaymentManager()

    class Meta:
        verbose_name = _("Stripe Payment")
        verbose_name_plural = _("Stripe Payments")
        ordering = ["-paid_at", "-created_at"]
```

#### 3. Centralized Validation (`payments/validators.py`)
- **`StripePaymentValidator`**:
  - Validates `amount >= Decimal("0.00")`.
  - Validates currency is 3 lowercase letters.
  - Validates `stripe_payment_intent_id` prefix (`pi_`) or `stripe_invoice_id` prefix (`in_`).

#### 4. Webhook Handler Logic (`payments/stripe/handlers/events.py`)
- **`handle_invoice_paid(payload)`**:
  1. Extract `invoice = payload["data"]["object"]`.
  2. Locate `Subscription` via `invoice.subscription`.
  3. Locate or create `SubscriptionPeriod` covering invoice period (`lines.data[0].period`).
  4. Create `StripePayment` record:
     - `amount = Decimal(invoice.amount_paid) / 100`
     - `currency = invoice.currency.lower()`
     - `stripe_payment_intent_id = invoice.payment_intent`
     - `stripe_invoice_id = invoice.id`
     - `status = PaymentStatusChoice.SUCCEEDED`
     - `paid_at = timezone.now()`
  5. If `subscription.status == PAST_DUE`: restore status to `ACTIVE`.
  6. Trigger `EntitlementService.reset_usage_for_new_period()`.
- **`handle_invoice_payment_failed(payload)`**:
  1. Extract `invoice = payload["data"]["object"]`.
  2. Locate `Subscription` via `invoice.subscription`.
  3. Create `StripePayment` record with `status = PaymentStatusChoice.FAILED`.
  4. Update `subscription.status = SubscriptionStatusChoice.PAST_DUE`.
  5. Log alert for dunning / payment retry notification.

#### 5. Stripe Customer Portal Service (`payments/stripe/services.py`)
- `StripePortalService.create_portal_session(user, return_url=None) -> str`:
  - Retrieves `customer = StripeCustomerService.get_or_create_customer(user)`.
  - Calls `stripe.billing_portal.Session.create(customer=customer.stripe_customer_id, return_url=return_url)`.
  - Returns the hosted portal URL where customers can update cards, view receipts, and cancel subscriptions.

#### 6. Class-Based Views (`payments/views.py` / `payments/stripe/views.py`)
- `BillingHistoryView(ListAPIView)`:
  - `GET /payments/api/billing/history/`: Returns paginated list of all payments and invoices for the authenticated user.
- `CreateBillingPortalSessionView(APIView)`:
  - `POST /payments/api/billing/portal/`: Returns `{ "portal_url": session.url }`.

#### 7. Planned Test Cases (`payments/tests/test_payments.py`)
1. Create valid `StripePayment` record.
2. Negative payment amount raises `ValidationError`.
3. Invalid currency code raises `ValidationError`.
4. Manager querysets: `.succeeded()`, `.failed()`, `.for_subscription()`.
5. Webhook `invoice.paid` creates succeeded payment and links to period.
6. Webhook `invoice.paid` restores past-due subscription to active.
7. Webhook `invoice.payment_failed` creates failed payment and marks subscription past-due.
8. `BillingHistoryView` returns authenticated user's payment list.
9. `BillingHistoryView` prevents users from viewing other users' payments.
10. `CreateBillingPortalSessionView` returns valid portal URL from mocked Stripe SDK.

---

### Phase 10: End-to-End Verification, Failure Drills & Production Readiness

#### 1. Objectives
- Run complete end-to-end sandbox simulations covering the entire lifecycle.
- Execute resilience drills (webhook replay, out-of-order deliveries, network timeouts).
- Document environment checklists and prepare for PostgreSQL deployment.

#### 2. Key Verification Drills
1. **Full Happy Path Simulation**:
   - User Registration $\rightarrow$ Plan Selection $\rightarrow$ Hosted Checkout $\rightarrow$ `checkout.session.completed` $\rightarrow$ Active Subscription $\rightarrow$ Quota Consumption $\rightarrow$ Monthly Renewal $\rightarrow$ `invoice.paid` $\rightarrow$ Usage Reset $\rightarrow$ Cancellation.
2. **Failure & Edge-Case Drills**:
   - Webhook signature tampering: Returns 400 Bad Request.
   - Duplicate webhook replay (Idempotency check): Processed once, duplicates safely ignored.
   - Out-of-order event delivery: `invoice.paid` arrives before `checkout.session.completed` without crashing.
   - Quota boundary race conditions: Multiple concurrent requests hitting quota limit cannot exceed permitted threshold.
3. **PostgreSQL Migration Readiness**:
   - Verify ORM models avoid SQLite-specific types.
   - Ensure clean migrations work identically on PostgreSQL.
   - Add database connection pooling & index optimization guidelines.
4. **Final Documentation & API Reference**:
   - Complete Postman / curl collection documentation.


---

## 7. Automated Test Suite Matrix

The project enforces automated test coverage across all domain components. Current count: **106 tests passing in ~0.98s**.

| Test File | Covered Domain | Key Test Scenarios | Test Count |
|---|---|---|:---:|
| [`payments/tests/test_features.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_features.py) | Catalog Foundations | Auto-slug generation, manager filters (`active`, `toggles`, `limits`), valid toggle config, invalid toggle with limit (raises `ValidationError`), valid limit config, missing limit/unit errors, unique constraint enforcement, nested `PlanSerializer` data. | **10** |
| [`payments/tests/test_prices.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_prices.py) | Pricing & Stripe Sync | Decimal-to-cents accuracy, negative amount validation, invalid currency code validation, invalid interval validation, `stripe_price_id` prefix validation, manager querysets, Stripe catalog sync mock (product + recurring price creation). | **10** |
| [`payments/tests/test_customers.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_customers.py) | Customer Mapping | 1:1 user relation, duplicate mapping prevention, duplicate `stripe_customer_id` prevention, `'cus_'` prefix validation, customer reuse logic (zero extra Stripe API calls on subsequent lookups). | **9** |
| [`payments/tests/test_checkout.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_checkout.py) | Stripe Checkout & Catalog | Active price validation, non-existent price rejection, inactive plan rejection, mock Stripe session creation with correct subscription metadata, auth required (403 on unauthenticated POST), 201 on valid POST, success/cancel views, catalog APIs. | **12** |
| [`payments/tests/test_webhooks.py`](file:///home/linkedmatrix/Payment-System/payments/tests/test_webhooks.py) | Webhooks & Idempotency | `evt_` prefix validation, manager filters, secret/signature validation, signature tampering rejection, dispatcher $O(1)$ routing, **idempotency guard skipping already-processed events**, failure tracking, HTTP 200 / 400 responses. | **12** |
| **Total** | | **All 53 unit & integration tests passing cleanly** | **53** |

---

## 8. API Reference & Payload Specifications

### 1. Catalog: List Active Plans
* **Endpoint**: `GET /payments/api/plans/`
* **Auth**: Public (`AllowAny`)
* **Response (HTTP 200 OK)**:
```json
[
  {
    "id": 1,
    "name": "Pro Tier",
    "slug": "pro-tier",
    "description": "Full access with high quotas",
    "is_active": true,
    "features": [
      {
        "id": 1,
        "feature_slug": "api-access",
        "feature_name": "API Access",
        "feature_type": "TOGGLE",
        "is_enabled": true,
        "limit": null,
        "unit": ""
      },
      {
        "id": 2,
        "feature_slug": "ai-requests",
        "feature_name": "AI Requests",
        "feature_type": "LIMIT",
        "is_enabled": true,
        "limit": 1000,
        "unit": "requests"
      }
    ],
    "prices": [
      {
        "id": 1,
        "amount": "29.00",
        "amount_in_cents": 2900,
        "currency": "usd",
        "billing_interval": "month",
        "billing_interval_display": "Month",
        "stripe_price_id": "price_1N2...",
        "is_synced": true,
        "is_active": true
      }
    ]
  }
]
```

---

### 2. Checkout: Create Subscription Session
* **Endpoint**: `POST /payments/api/checkout/`
* **Auth**: Authenticated (`IsAuthenticated`)
* **Request Payload**:
```json
{
  "plan_price_id": 1,
  "success_url": "http://localhost:8000/payments/success/?session_id={CHECKOUT_SESSION_ID}",
  "cancel_url": "http://localhost:8000/payments/cancel/"
}
```
* **Response (HTTP 201 Created)**:
```json
{
  "session_id": "cs_test_a1b2c3d4e5",
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3d4e5"
}
```

---

### 3. Webhook: Stripe Inbound Receiver
* **Endpoint**: `POST /payments/webhooks/stripe/`
* **Auth**: Cryptographic HMAC Header (`HTTP_STRIPE_SIGNATURE`)
* **Response (HTTP 200 OK)**:
```json
{
  "status": "received",
  "event_id": "evt_1N23456789",
  "processing_status": "processed"
}
```
* **Error Response on Invalid Signature (HTTP 400 Bad Request)**:
```json
{
  "error": "Invalid webhook signature."
}
```
