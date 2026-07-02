"""Tests for provider integrations (stubs)."""
import pytest
from mcp_payments.engine import PaymentEngine
from mcp_payments.models import Currency, PaymentProvider, PaymentStatus
from mcp_payments.storage import Storage


@pytest.fixture
def engine(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage, merchant_wallet="0xMerchant")


class TestProviders:
    def test_internal_provider_success(self, engine):
        c = engine.create_customer(name="Test", )
        engine.top_up_balance(c.id, 10000)
        p = engine.charge(c.id, 500, provider=PaymentProvider.INTERNAL)
        assert p.status == PaymentStatus.SUCCEEDED
        assert p.provider_transaction_id.startswith("int_")

    def test_x402_provider_with_wallet(self, engine):
        c = engine.create_customer(name="Test", wallet_address="0xWallet")
        p = engine.charge(c.id, 500, provider=PaymentProvider.X402)
        assert p.status == PaymentStatus.SUCCEEDED
        assert p.provider_transaction_id.startswith("x402_")

    def test_x402_provider_no_wallet(self, engine):
        c = engine.create_customer(name="Test")
        p = engine.charge(c.id, 500, provider=PaymentProvider.X402)
        assert p.status == PaymentStatus.FAILED

    def test_stripe_provider_pending(self, engine):
        c = engine.create_customer(name="Test")
        p = engine.charge(c.id, 500, provider=PaymentProvider.STRIPE)
        # Stripe is a stub — should be pending
        assert p.status == PaymentStatus.PENDING
        assert p.provider_transaction_id.startswith("stripe_")

    def test_solana_provider_with_wallet(self, engine):
        c = engine.create_customer(name="Test", wallet_address="SolWallet123")
        p = engine.charge(c.id, 500, provider=PaymentProvider.SOLANA)
        assert p.status == PaymentStatus.SUCCEEDED
        assert p.provider_transaction_id.startswith("chain_")

    def test_solana_provider_no_wallet(self, engine):
        c = engine.create_customer(name="Test")
        p = engine.charge(c.id, 500, provider=PaymentProvider.SOLANA)
        assert p.status == PaymentStatus.FAILED

    def test_ethereum_provider_with_wallet(self, engine):
        c = engine.create_customer(name="Test", wallet_address="0xEthWallet")
        p = engine.charge(c.id, 500, provider=PaymentProvider.ETHEREUM)
        assert p.status == PaymentStatus.SUCCEEDED

    def test_lightning_provider_with_wallet(self, engine):
        c = engine.create_customer(name="Test", wallet_address="lnbc1wallet")
        p = engine.charge(c.id, 500, provider=PaymentProvider.LIGHTNING)
        assert p.status == PaymentStatus.SUCCEEDED

    def test_provider_transaction_ids_unique(self, engine):
        c = engine.create_customer(name="Test", wallet_address="0xWallet")
        engine.top_up_balance(c.id, 100000)
        p1 = engine.charge(c.id, 100)
        p2 = engine.charge(c.id, 100)
        assert p1.provider_transaction_id != p2.provider_transaction_id
