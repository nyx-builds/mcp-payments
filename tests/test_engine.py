"""Tests for the payment engine."""
import pytest
from mcp_payments.engine import PaymentEngine
from mcp_payments.models import (
    Currency,
    PaymentProvider,
    PaymentStatus,
    PricingModel,
)
from mcp_payments.storage import Storage


@pytest.fixture
def engine(tmp_path):
    """Fresh engine with temp storage."""
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def engine_with_wallet(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage, merchant_wallet="0xMerchant123")


@pytest.fixture
def customer(engine):
    return engine.create_customer(name="Test Agent", agent_id="said:test123")


@pytest.fixture
def customer_with_wallet(engine_with_wallet):
    return engine_with_wallet.create_customer(
        name="Crypto Agent",
        wallet_address="0xWallet456",
    )


# ── Pricing Tests ──────────────────────────────────────────────────────────

class TestPricing:
    def test_set_price(self, engine):
        pricing = engine.set_price("search", 50, Currency.USD, PricingModel.PER_USE)
        assert pricing.tool_name == "search"
        assert pricing.price.amount == 50

    def test_get_price(self, engine):
        engine.set_price("search", 50)
        pricing = engine.get_price("search")
        assert pricing is not None
        assert pricing.price.amount == 50

    def test_get_nonexistent_price(self, engine):
        assert engine.get_price("nonexistent") is None

    def test_list_prices(self, engine):
        engine.set_price("search", 50)
        engine.set_price("translate", 100)
        prices = engine.list_prices()
        assert len(prices) == 2

    def test_free_tier_no_usage(self, engine, customer):
        engine.set_price("search", 50, free_tier_limit=10)
        assert engine.check_free_tier("search", customer.id) is True

    def test_free_tier_exhausted(self, engine, customer):
        engine.set_price("search", 50, free_tier_limit=1)
        engine.top_up_balance(customer.id, 10000)

        # First call — free
        p1 = engine.charge(customer.id, 0, tool_name="search")
        assert p1.status == PaymentStatus.SUCCEEDED

        # Free tier now exhausted
        assert engine.check_free_tier("search", customer.id) is False

    def test_no_free_tier(self, engine, customer):
        engine.set_price("search", 50)
        assert engine.check_free_tier("search", customer.id) is False


# ── Customer Tests ─────────────────────────────────────────────────────────

class TestCustomer:
    def test_create_customer(self, engine):
        c = engine.create_customer(name="Agent Smith")
        assert c.id.startswith("cus_")
        assert c.name == "Agent Smith"

    def test_create_customer_with_wallet(self, engine):
        c = engine.create_customer(wallet_address="0xABC")
        assert c.wallet_address == "0xABC"

    def test_get_customer(self, engine, customer):
        fetched = engine.get_customer(customer.id)
        assert fetched is not None
        assert fetched.name == "Test Agent"

    def test_get_nonexistent_customer(self, engine):
        assert engine.get_customer("cus_nonexistent") is None

    def test_top_up_balance(self, engine, customer):
        updated = engine.top_up_balance(customer.id, 5000)
        assert updated.balance == 5000

    def test_top_up_accumulates(self, engine, customer):
        engine.top_up_balance(customer.id, 1000)
        engine.top_up_balance(customer.id, 500)
        customer_updated = engine.get_customer(customer.id)
        assert customer_updated.balance == 1500


# ── Payment Tests ──────────────────────────────────────────────────────────

class TestPayment:
    def test_successful_internal_payment(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500, description="API call")
        assert payment.status == PaymentStatus.SUCCEEDED
        assert payment.amount == 500
        assert payment.provider == PaymentProvider.INTERNAL

    def test_insufficient_balance(self, engine, customer):
        engine.top_up_balance(customer.id, 100)
        payment = engine.charge(customer.id, 500)
        assert payment.status == PaymentStatus.FAILED
        assert "Insufficient balance" in payment.failure_reason

    def test_nonexistent_customer(self, engine):
        with pytest.raises(ValueError):
            engine.charge("cus_nonexistent", 100)

    def test_balance_deducted_after_charge(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        engine.charge(customer.id, 500)
        customer_after = engine.get_customer(customer.id)
        assert customer_after.balance == 9500

    def test_free_tier_zero_payment(self, engine, customer):
        engine.set_price("search", 50, free_tier_limit=5)
        payment = engine.charge(customer.id, 0, tool_name="search")
        assert payment.status == PaymentStatus.SUCCEEDED
        assert payment.amount == 0

    def test_payment_with_tool_name(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 100, tool_name="search")
        assert payment.tool_name == "search"

    def test_payment_with_metadata(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 100, metadata={"request_id": "req_123"})
        assert payment.metadata["request_id"] == "req_123"


# ── Refund Tests ───────────────────────────────────────────────────────────

class TestRefund:
    def test_full_refund(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500)
        assert payment.status == PaymentStatus.SUCCEEDED

        # Balance after charge: 9500
        refund = engine.refund(payment.id)
        assert refund.status.value == "succeeded"
        assert refund.amount == 500

        # Balance restored
        customer_after = engine.get_customer(customer.id)
        assert customer_after.balance == 10000

    def test_partial_refund(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500)

        refund = engine.refund(payment.id, amount=200)
        assert refund.amount == 200

        # Check payment updated
        payment_updated = engine.storage.get_payment(payment.id)
        assert payment_updated.refund_amount == 200
        assert payment_updated.status != PaymentStatus.REFUNDED  # Still succeeded (partial)

    def test_refund_exceeds_amount(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500)

        with pytest.raises(ValueError, match="exceeds"):
            engine.refund(payment.id, amount=600)

    def test_refund_non_succeeded_payment(self, engine, customer):
        engine.top_up_balance(customer.id, 100)
        payment = engine.charge(customer.id, 500)  # Fails: insufficient
        with pytest.raises(ValueError, match="Cannot refund"):
            engine.refund(payment.id)


# ── Intent Tests ───────────────────────────────────────────────────────────

class TestIntents:
    def test_create_intent(self, engine, customer):
        intent = engine.create_intent(customer.id, 500)
        assert intent.id.startswith("pi_")
        assert intent.status == PaymentStatus.PENDING

    def test_fulfill_intent(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        intent = engine.create_intent(customer.id, 500)
        payment = engine.fulfill_intent(intent.id)
        assert payment is not None
        assert payment.status == PaymentStatus.SUCCEEDED

    def test_fulfill_expired_intent(self, engine, customer):
        intent = engine.create_intent(customer.id, 500, expires_in_seconds=-1)
        result = engine.fulfill_intent(intent.id)
        assert result is None

    def test_fulfill_already_fulfilled(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        intent = engine.create_intent(customer.id, 500)
        engine.fulfill_intent(intent.id)
        result = engine.fulfill_intent(intent.id)
        assert result is None


# ── x402 Tests ─────────────────────────────────────────────────────────────

class TestX402:
    def test_create_x402_requirements(self, engine_with_wallet):
        req = engine_with_wallet.create_x402_requirements(
            amount=0.01,
            currency=Currency.USD,
            resource_url="https://api.example.com/premium",
        )
        assert req.pay_to == "0xMerchant123"
        assert req.scheme == "exact"
        assert req.network == "base-sepolia"
        # $0.01 = 10000 atomic USDC units (6 decimals)
        assert int(req.amount) == 10000

    def test_x402_requires_merchant_wallet(self, engine):
        with pytest.raises(ValueError, match="merchant_wallet"):
            engine.create_x402_requirements(amount=1.0, currency=Currency.USD)

    def test_x402_with_wallet_payment(self, engine_with_wallet, customer_with_wallet):
        payment = engine_with_wallet.charge(
            customer_with_wallet.id,
            50,
            provider=PaymentProvider.X402,
        )
        assert payment.status == PaymentStatus.SUCCEEDED
        assert payment.provider_transaction_id.startswith("x402_")

    def test_x402_without_wallet_fails(self, engine_with_wallet):
        c = engine_with_wallet.create_customer(name="No Wallet")
        payment = engine_with_wallet.charge(c.id, 50, provider=PaymentProvider.X402)
        assert payment.status == PaymentStatus.FAILED
        assert "wallet" in payment.failure_reason.lower()


# ── Verify & Receipt Tests ─────────────────────────────────────────────────

class TestVerifyAndReceipt:
    def test_verify_succeeded_payment(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500)
        result = engine.verify_payment(payment.id)
        assert result["valid"] is True
        assert result["status"] == "succeeded"

    def test_verify_nonexistent_payment(self, engine):
        result = engine.verify_payment("pay_nonexistent")
        assert result["valid"] is False

    def test_get_receipt(self, engine, customer):
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500)
        receipt = engine.get_receipt(payment.id)
        assert receipt is not None
        assert receipt.payment_id == payment.id
        assert receipt.signature is not None

    def test_receipt_for_failed_payment(self, engine, customer):
        engine.top_up_balance(customer.id, 100)
        payment = engine.charge(customer.id, 500)
        receipt = engine.get_receipt(payment.id)
        assert receipt is None


# ── Summary & Analytics ────────────────────────────────────────────────────

class TestSummary:
    def test_empty_summary(self, engine):
        data = engine.summary()
        assert data["total_payments"] == 0
        assert data["total_volume"] == 0

    def test_summary_with_payments(self, engine, customer):
        engine.top_up_balance(customer.id, 100000)
        engine.charge(customer.id, 500, tool_name="search")
        engine.charge(customer.id, 300, tool_name="translate")

        data = engine.summary()
        assert data["total_payments"] == 2
        assert data["succeeded"] == 2
        assert data["total_volume"] == 800
        assert "search" in data["by_tool"]
        assert data["by_tool"]["search"] == 500

    def test_summary_customer_filter(self, engine, customer):
        engine.top_up_balance(customer.id, 100000)
        engine.charge(customer.id, 500)

        other = engine.create_customer(name="Other")
        engine.top_up_balance(other.id, 100000)
        engine.charge(other.id, 300)

        data = engine.summary(customer_id=customer.id)
        assert data["total_payments"] == 1

    def test_webhook_signature(self, engine):
        payload = b'{"event":"payment.succeeded"}'
        secret = "whsec_test123"
        sig = engine.generate_webhook_signature(payload, secret)
        assert engine.verify_webhook(payload, sig, secret) is True

    def test_webhook_wrong_signature(self, engine):
        payload = b'{"event":"payment.succeeded"}'
        assert engine.verify_webhook(payload, "wrong_sig", "secret") is False


# ── Persistence Tests ──────────────────────────────────────────────────────

class TestPersistence:
    def test_reload_preserves_data(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)
        c = engine1.create_customer(name="Persisted")
        engine1.top_up_balance(c.id, 5000)

        # New engine instance, same data dir
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        loaded = engine2.get_customer(c.id)
        assert loaded is not None
        assert loaded.name == "Persisted"
        assert loaded.balance == 5000
