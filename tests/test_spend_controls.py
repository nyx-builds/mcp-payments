"""
Tests for v0.6.0 Agent Spend Controls.

Covers:
- SpendPolicy CRUD operations
- Per-transaction, daily, weekly, monthly limits
- Tool allowlists/blocklists
- Rate limiting
- Authorization checks (pre-auth)
- Spend reports
- Integration with charge() — denied charges create FAILED payments
- Multiple policies (most restrictive wins)
- Policy updates by name
"""

import pytest
from datetime import datetime, timezone

from mcp_payments.models import (
    AuthorizationStatus,
    Currency,
    PaymentStatus,
    PaymentProvider,
    SpendPolicy,
)
from mcp_payments.engine import PaymentEngine
from mcp_payments.storage import Storage


@pytest.fixture
def engine(tmp_path):
    """Fresh engine with temp storage."""
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def customer(engine):
    return engine.create_customer(
        name="Test Agent",
        email="agent@test.com",
    )


# ── SpendPolicy CRUD ──────────────────────────────────────────────────

class TestSpendPolicyCRUD:
    def test_create_policy(self, engine, customer):
        policy = engine.set_spend_policy(
            customer_id=customer.id,
            daily_limit=1000,
            monthly_limit=10000,
        )
        assert policy.id is not None
        assert policy.customer_id == customer.id
        assert policy.daily_limit == 1000
        assert policy.monthly_limit == 10000
        assert policy.enabled is True

    def test_get_policy(self, engine, customer):
        policy = engine.set_spend_policy(
            customer_id=customer.id,
            daily_limit=500,
        )
        fetched = engine.get_spend_policy(policy.id)
        assert fetched is not None
        assert fetched.id == policy.id
        assert fetched.daily_limit == 500

    def test_list_all_policies(self, engine, customer):
        engine.set_spend_policy(customer_id=customer.id, name="strict", daily_limit=100)
        engine.set_spend_policy(customer_id=customer.id, name="loose", daily_limit=10000)
        policies = engine.list_spend_policies()
        assert len(policies) == 2

    def test_list_policies_by_customer(self, engine):
        c1 = engine.create_customer(name="Agent1", email="a1@test.com")
        c2 = engine.create_customer(name="Agent2", email="a2@test.com")
        engine.set_spend_policy(customer_id=c1.id, daily_limit=100)
        engine.set_spend_policy(customer_id=c2.id, daily_limit=200)
        c1_policies = engine.list_spend_policies(customer_id=c1.id)
        assert len(c1_policies) == 1
        assert c1_policies[0].customer_id == c1.id

    def test_list_policies_by_enabled(self, engine, customer):
        engine.set_spend_policy(customer_id=customer.id, name="active", daily_limit=100, enabled=True)
        engine.set_spend_policy(customer_id=customer.id, name="disabled", daily_limit=200, enabled=False)
        enabled = engine.list_spend_policies(enabled=True)
        assert len(enabled) == 1
        assert enabled[0].name == "active"

    def test_delete_policy(self, engine, customer):
        policy = engine.set_spend_policy(customer_id=customer.id, daily_limit=100)
        assert engine.delete_spend_policy(policy.id) is True
        assert engine.get_spend_policy(policy.id) is None

    def test_delete_nonexistent_policy(self, engine):
        assert engine.delete_spend_policy("nonexistent") is False

    def test_policy_for_nonexistent_customer(self, engine):
        with pytest.raises(ValueError, match="Customer not found"):
            engine.set_spend_policy(customer_id="fake-id", daily_limit=100)


class TestSpendPolicyUpdate:
    def test_update_by_name(self, engine, customer):
        # Create initial policy
        engine.set_spend_policy(
            customer_id=customer.id,
            name="default",
            daily_limit=500,
        )
        # Update same policy by name
        updated = engine.set_spend_policy(
            customer_id=customer.id,
            name="default",
            daily_limit=2000,
        )
        assert updated.daily_limit == 2000
        # Should only have 1 policy, not 2
        policies = engine.list_spend_policies(customer_id=customer.id)
        assert len(policies) == 1

    def test_update_preserves_other_fields(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            name="default",
            daily_limit=500,
            monthly_limit=5000,
        )
        # Update only daily_limit
        updated = engine.set_spend_policy(
            customer_id=customer.id,
            name="default",
            daily_limit=1000,
        )
        assert updated.daily_limit == 1000
        assert updated.monthly_limit == 5000  # preserved


# ── Authorization Checks ──────────────────────────────────────────────

class TestAuthorization:
    def test_no_policy_allows_charge(self, engine, customer):
        result = engine.check_authorization(
            customer_id=customer.id,
            amount=999999,
        )
        assert result.authorized is True
        assert result.status == AuthorizationStatus.APPROVED

    def test_per_transaction_limit_pass(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=1000,
        )
        result = engine.check_authorization(customer_id=customer.id, amount=500)
        assert result.authorized is True

    def test_per_transaction_limit_deny(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=1000,
        )
        result = engine.check_authorization(customer_id=customer.id, amount=1500)
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_OVER_PER_TRANSACTION
        assert "per-transaction" in result.reason

    def test_per_transaction_exact_limit(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=1000,
        )
        result = engine.check_authorization(customer_id=customer.id, amount=1000)
        assert result.authorized is True  # equal is OK

    def test_daily_limit_accumulates(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            daily_limit=1000,
        )
        # Top up and charge
        engine.top_up_balance(customer.id, 10000)
        engine.charge(customer.id, 600, tool_name="tool-a")
        # Should still be able to charge 400
        result = engine.check_authorization(customer_id=customer.id, amount=400)
        assert result.authorized is True
        # But 401 should fail
        result = engine.check_authorization(customer_id=customer.id, amount=401)
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_OVER_DAILY_LIMIT

    def test_monthly_limit(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            monthly_limit=5000,
        )
        engine.top_up_balance(customer.id, 100000)
        engine.charge(customer.id, 4000, tool_name="tool-a")
        result = engine.check_authorization(customer_id=customer.id, amount=2000)
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_OVER_MONTHLY_LIMIT

    def test_blocked_tool(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            blocked_tools=["dangerous-tool"],
        )
        result = engine.check_authorization(
            customer_id=customer.id,
            amount=100,
            tool_name="dangerous-tool",
        )
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_TOOL_BLOCKED

    def test_allowed_tool_whitelist(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            allowed_tools=["safe-tool-1", "safe-tool-2"],
        )
        # Allowed tool passes
        result = engine.check_authorization(
            customer_id=customer.id,
            amount=100,
            tool_name="safe-tool-1",
        )
        accumulation_result = result
        assert accumulation_result.authorized is True
        # Non-whitelisted tool fails
        result = engine.check_authorization(
            customer_id=customer.id,
            amount=100,
            tool_name="unlisted-tool",
        )
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_TOOL_NOT_ALLOWED

    def test_rate_limit(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_transactions_per_hour=3,
        )
        engine.top_up_balance(customer.id, 100000)
        # Make 3 charges
        for _ in range(3):
            engine.charge(customer.id, 10, tool_name="tool-a")
        # 4th should be rate limited
        result = engine.check_authorization(customer_id=customer.id, amount=10)
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_RATE_LIMITED

    def test_disabled_policy_not_checked(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=100,
            enabled=False,
        )
        result = engine.check_authorization(customer_id=customer.id, amount=99999)
        assert result.authorized is True

    def test_multiple_policies_most_restrictive(self, engine, customer):
        # Two policies: one with daily limit, one with per-transaction
        engine.set_spend_policy(
            customer_id=customer.id,
            name="daily-cap",
            daily_limit=1000,
        )
        engine.set_spend_policy(
            customer_id=customer.id,
            name="per-tx-cap",
            max_per_transaction=50,
        )
        # Charge of 75: passes daily, fails per-transaction
        result = engine.check_authorization(customer_id=customer.id, amount=75)
        assert result.authorized is False
        assert result.status == AuthorizationStatus.DENIED_OVER_PER_TRANSACTION


# ── Charge Integration ────────────────────────────────────────────────

class TestChargeIntegration:
    def test_denied_charge_creates_failed_payment(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=100,
        )
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 500, tool_name="tool-a")
        assert payment.status == PaymentStatus.FAILED
        assert payment.failure_reason is not None
        assert "per-transaction" in payment.failure_reason
        assert payment.metadata["authorization_status"] == "denied_over_per_transaction"

    def test_denied_charge_does_not_deduct_balance(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=100,
        )
        engine.top_up_balance(customer.id, 10000)
        balance_before = engine.get_customer(customer.id).balance
        engine.charge(customer.id, 500)
        balance_after = engine.get_customer(customer.id).balance
        assert balance_before == balance_after

    def test_approved_charge_deducts_balance(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            max_per_transaction=100,
        )
        engine.top_up_balance(customer.id, 10000)
        engine.charge(customer.id, 50, tool_name="tool-a")
        assert engine.get_customer(customer.id).balance == 9950

    def test_blocked_tool_charge_fails(self, engine, customer):
        engine.set_spend_policy(
            customer_id=customer.id,
            blocked_tools=["expensive-api"],
        )
        engine.top_up_balance(customer.id, 10000)
        payment = engine.charge(customer.id, 10, tool_name="expensive-api")
        assert payment.status == PaymentStatus.FAILED
        assert "blocked" in payment.failure_reason.lower()


# ── Spend Reports ─────────────────────────────────────────────────────

class TestSpendReport:
    def test_empty_report(self, engine, customer):
        report = engine.get_spend_report(customer_id=customer.id)
        assert report.total_spend == 0
        assert report.total_transactions == 0
        assert report.total_refunded == 0
        assert report.net_spend == 0
        assert report.by_tool == {}
        assert report.by_day == {}

    def test_report_with_charges(self, engine, customer):
        engine.top_up_balance(customer.id, 100000)
        engine.charge(customer.id, 100, tool_name="tool-a")
        engine.charge(customer.id, 200, tool_name="tool-b")
        engine.charge(customer.id, 50, tool_name="tool-a")

        report = engine.get_spend_report(customer_id=customer.id)
        assert report.total_spend == 350
        assert report.total_transactions == 3
        assert report.by_tool["tool-a"] == 150
        assert report.by_tool["tool-b"] == 200
        assert report.average_transaction == pytest.approx(116.67, rel=0.01)
        assert report.largest_transaction == 200

    def test_report_net_after_refund(self, engine, customer):
        engine.top_up_balance(customer.id, 100000)
        payment = engine.charge(customer.id, 1000, tool_name="tool-a")
        engine.refund(payment.id, amount=400)

        report = engine.get_spend_report(customer_id=customer.id)
        assert report.total_spend == 1000
        assert report.total_refunded == 400
        assert report.net_spend == 600

    def test_report_includes_policy_ids(self, engine, customer):
        policy = engine.set_spend_policy(
            customer_id=customer.id,
            daily_limit=10000,
        )
        report = engine.get_spend_report(customer_id=customer.id)
        assert policy.id in report.policies_applied


# ── Storage Persistence ───────────────────────────────────────────────

class TestPersistence:
    def test_policy_persists_to_disk(self, tmp_path):
        storage = Storage(data_dir=str(tmp_path / "payments"))
        engine = PaymentEngine(storage=storage)
        customer = engine.create_customer(name="Persist Test", email="p@test.com")
        engine.set_spend_policy(customer_id=customer.id, daily_limit=500)

        # New storage instance reads from same file
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        policies = engine2.list_spend_policies(customer_id=customer.id)
        assert len(policies) == 1
        assert policies[0].daily_limit == 500
