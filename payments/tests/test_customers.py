"""
Automated Tests for Milestone 4: StripeCustomer, User Mapping, and Stripe Customer Service.
"""

from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from payments.models import StripeCustomer
from payments.serializers import StripeCustomerSerializer

from payments.stripe.services import StripeCustomerService

User = get_user_model()


class StripeCustomerModelTest(TestCase):
    """Tests for the StripeCustomer model and validator."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="john_doe",
            email="john@example.com",
            first_name="John",
            last_name="Doe",
        )

    def test_create_valid_stripe_customer(self):
        customer = StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_test_12345",
        )
        self.assertEqual(customer.user, self.user)
        self.assertEqual(customer.stripe_customer_id, "cus_test_12345")
        self.assertEqual(str(customer), "john_doe (cus_test_12345)")
        # User reverse relation
        self.assertEqual(self.user.stripe_customer, customer)

    def test_one_to_one_constraint_prevents_duplicate_mapping(self):
        StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_first_123",
        )
        with self.assertRaises((ValidationError, IntegrityError)):
            StripeCustomer.objects.create(
                user=self.user,
                stripe_customer_id="cus_second_456",
            )

    def test_duplicate_stripe_customer_id_raises_integrity_error(self):
        user2 = User.objects.create_user(username="jane_doe", email="jane@example.com")
        StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_unique_id",
        )
        with self.assertRaises((ValidationError, IntegrityError)):
            StripeCustomer.objects.create(
                user=user2,
                stripe_customer_id="cus_unique_id",
            )


    def test_invalid_customer_id_prefix_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            StripeCustomer.objects.create(
                user=self.user,
                stripe_customer_id="invalid_prefix_12345",
            )
        self.assertIn("stripe_customer_id", ctx.exception.message_dict)

    def test_empty_customer_id_raises_validation_error(self):
        with self.assertRaises(ValidationError) as ctx:
            StripeCustomer.objects.create(
                user=self.user,
                stripe_customer_id="",
            )
        self.assertIn("stripe_customer_id", ctx.exception.message_dict)

    def test_customer_manager_queries(self):
        customer = StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_manager_test",
        )
        self.assertEqual(
            StripeCustomer.objects.for_user(self.user).first(),
            customer,
        )
        self.assertEqual(
            StripeCustomer.objects.by_stripe_id("cus_manager_test").first(),
            customer,
        )


class StripeCustomerSerializerTest(TestCase):
    """Tests for StripeCustomerSerializer."""

    def test_serializer_output(self):
        user = User.objects.create_user(username="alex", email="alex@example.com")
        customer = StripeCustomer.objects.create(
            user=user,
            stripe_customer_id="cus_alex_999",
        )
        serializer = StripeCustomerSerializer(customer)
        data = serializer.data
        self.assertEqual(data["user_username"], "alex")
        self.assertEqual(data["user_email"], "alex@example.com")
        self.assertEqual(data["stripe_customer_id"], "cus_alex_999")


class StripeCustomerServiceTest(TestCase):
    """Tests for StripeCustomerService with mocked Stripe SDK."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="testuser@example.com",
            first_name="Jane",
            last_name="Smith",
        )

    @patch("stripe.Customer.create")
    def test_get_or_create_customer_creates_new_when_not_exists(self, mock_stripe_create):
        mock_stripe_res = MagicMock()
        mock_stripe_res.id = "cus_stripe_new_789"
        mock_stripe_create.return_value = mock_stripe_res

        customer = StripeCustomerService.get_or_create_customer(self.user)

        self.assertEqual(customer.stripe_customer_id, "cus_stripe_new_789")
        self.assertEqual(customer.user, self.user)
        self.assertTrue(StripeCustomer.objects.filter(user=self.user).exists())

        mock_stripe_create.assert_called_once_with(
            email="testuser@example.com",
            name="Jane Smith",
            metadata={
                "django_user_id": str(self.user.id),
                "username": "testuser",
            },
        )

    @patch("stripe.Customer.create")
    def test_get_or_create_customer_reuses_existing_without_stripe_call(self, mock_stripe_create):
        existing_customer = StripeCustomer.objects.create(
            user=self.user,
            stripe_customer_id="cus_existing_456",
        )

        customer = StripeCustomerService.get_or_create_customer(self.user)

        self.assertEqual(customer.id, existing_customer.id)
        self.assertEqual(customer.stripe_customer_id, "cus_existing_456")
        mock_stripe_create.assert_not_called()
