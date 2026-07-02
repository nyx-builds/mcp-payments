"""Tests for mcp_payments core models."""
import pytest
from mcp_payments.models import (
    Currency,
    Customer,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Price,
    PricingModel,
    ToolPricing,
    Refund,
    RefundStatus,
    PaymentIntent,
    PaymentReceipt,
    X402PaymentRequirements,
)


class TestPrice:
    def test_usd_display(self):
        p = Price(amount=9.99, currency=Currency.USD)
        assert p.display() == "$9.99"

    def test_eur_display(self):
        p = Price(amount=15.50, currency=Currency.EUR)
        assert p.display() == "€15.50"

    def test_crypto_display(self):
        p = Price(amount=0.001, currency=Currency.USDC)
        assert "USDC" in p.display()

    def test_negative_amount_rejected(self):
        with pytest.raises(Exception):
            Price(amount=-1)

    def test_default_pricing_model(self):
        p = Price(amount=10)
        assert p.pricing_model == PricingModel.FIXED


class TestCustomer:
    def test_default_id_generation(self):
        c = Customer()
        assert c.id.startswith("cus_")

    def test_unique_ids(self):
        c1 = Customer()
        c2 = Customer()
        assert c1.id != c2.id

    def test_default_balance(self):
        c = Customer()
        assert c.balance == 0.0

    def test_agent_id_optional(self):
        c = Customer(agent_id="said:abc123")
        assert c.agent_id == "said:abc123"


class TestPayment:
    def test_default_id_generation(self):
        p = Payment(customer_id="cus_123", amount=100)
        assert p.id.startswith("pay_")

    def test_default_status_pending(self):
        p = Payment(customer_id="cus_123", amount=100)
        assert p.status == PaymentStatus.PENDING

    def test_default_provider_internal(self):
        p = Payment(customer_id="cus_123", amount=100)
        assert p.provider == PaymentProvider.INTERNAL

    def test_negative_amount_rejected(self):
        with pytest.raises(Exception):
            Payment(customer_id="cus_123", amount=-50)

    def test_refund_amount_default(self):
        p = Payment(customer_id="cus_123", amount=100)
        assert p.refund_amount == 0.0


class TestToolPricing:
    def test_creation(self):
        pricing = ToolPricing(
            tool_name="search",
            price=Price(amount=5),
        )
        assert pricing.tool_name == "search"
        assert pricing.price.amount == 5
        assert pricing.enabled is True

    def test_free_tier_limit(self):
        pricing = ToolPricing(
            tool_name="search",
            price=Price(amount=5),
            free_tier_limit=100,
        )
        assert pricing.free_tier_limit == 100


class TestRefund:
    def test_default_id(self):
        r = Refund(payment_id="pay_123", amount=50)
        assert r.id.startswith("ref_")

    def test_default_status_pending(self):
        r = Refund(payment_id="pay_123", amount=50)
        assert r.status == RefundStatus.PENDING


class TestPaymentIntent:
    def test_default_id(self):
        intent = PaymentIntent(customer_id="cus_123", amount=100)
        assert intent.id.startswith("pi_")

    def test_default_status_pending(self):
        intent = PaymentIntent(customer_id="cus_123", amount=100)
        assert intent.status == PaymentStatus.PENDING


class TestX402Requirements:
    def test_creation(self):
        req = X402PaymentRequirements(
            amount="1000000",
            pay_to="0xABC123",
        )
        assert req.scheme == "exact"
        assert req.network == "base-sepolia"
