"""Tests for v0.2.0 features: Escrow and Split Payments.

Escrow solves the agent-to-agent trust problem — Agent A funds escrow,
Agent B does the work, Agent A releases (or funds are auto-refunded).

Split Payments allow one charge to be distributed to multiple recipients —
essential for marketplace/multi-party agent economies.
"""
import pytest
from datetime import datetime, timezone

from mcp_payments.engine import PaymentEngine
from mcp_payments.models import (
    Currency,
    EscrowStatus,
    PaymentStatus,
    SplitStatus,
)
from mcp_payments.server import MCPServer
from mcp_payments.storage import Storage


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def engine(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def server(engine):
    return MCPServer(engine=engine)


@pytest.fixture
def two_customers(engine):
    """Create a payer with balance and a payee."""
    payer = engine.create_customer(name="Payer Agent", agent_id="said:payer")
    payee = engine.create_customer(name="Payee Agent", agent_id="said:payee")
    engine.top_up_balance(payer.id, 10000.0)  # $100 in cents
    return payer, payee


# ── Escrow: Creation ────────────────────────────────────────────────────────

class TestEscrowCreation:
    def test_create_escrow_holds_funds(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=500.0,
            task_description="Summarize 10 articles",
        )
        assert escrow.status == EscrowStatus.HELD
        assert escrow.amount == 500.0
        assert escrow.payer_customer_id == payer.id
        assert escrow.payee_customer_id == payee.id
        assert escrow.payment_id is not None
        # Payer's balance should be reduced by escrow amount
        updated_payer = engine.get_customer(payer.id)
        assert updated_payer.balance == 9500.0  # 10000 - 500
        # Payee should NOT have the funds yet
        updated_payee = engine.get_customer(payee.id)
        assert updated_payee.balance == 0.0

    def test_create_escrow_validates_payer_exists(self, engine, two_customers):
        _, payee = two_customers
        with pytest.raises(ValueError, match="Payer not found"):
            engine.create_escrow(
                payer_customer_id="cus_nonexistent",
                payee_customer_id=payee.id,
                amount=100.0,
            )

    def test_create_escrow_validates_payee_exists(self, engine, two_customers):
        payer, _ = two_customers
        with pytest.raises(ValueError, match="Payee not found"):
            engine.create_escrow(
                payer_customer_id=payer.id,
                payee_customer_id="cus_nonexistent",
                amount=100.0,
            )

    def test_create_escrow_rejects_same_payer_payee(self, engine, two_customers):
        payer, _ = two_customers
        with pytest.raises(ValueError, match="must be different"):
            engine.create_escrow(
                payer_customer_id=payer.id,
                payee_customer_id=payer.id,
                amount=100.0,
            )

    def test_create_escrow_fails_on_insufficient_balance(self, engine):
        payer = engine.create_customer(name="Poor Payer")
        payee = engine.create_customer(name="Payee")
        with pytest.raises(ValueError, match="Escrow funding failed"):
            engine.create_escrow(
                payer_customer_id=payer.id,
                payee_customer_id=payee.id,
                amount=1000.0,
            )

    def test_create_escrow_with_expiry(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=200.0,
            task_description="Quick task",
            expires_in_seconds=3600,
        )
        assert escrow.expires_at is not None
        assert escrow.expires_at > datetime.now(timezone.utc)

    def test_create_escrow_with_metadata(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=100.0,
            task_description="Task",
            task_id="job_123",
            metadata={"project": "alpha"},
        )
        assert escrow.task_id == "job_123"
        assert escrow.metadata["project"] == "alpha"


# ── Escrow: Release ─────────────────────────────────────────────────────────

class TestEscrowRelease:
    def test_release_escrow_credits_payee(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=500.0,
            task_description="Do the work",
        )
        released = engine.release_escrow(escrow.id)
        assert released.status == EscrowStatus.RELEASED
        assert released.released_at is not None
        assert released.release_payment_id is not None
        # Payee now has the funds
        updated_payee = engine.get_customer(payee.id)
        assert updated_payee.balance == 500.0

    def test_release_escrow_records_payment(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=300.0,
        )
        released = engine.release_escrow(escrow.id)
        # The release should have created a payment record
        release_pay = engine.storage.get_payment(released.release_payment_id)
        assert release_pay is not None
        assert release_pay.status == PaymentStatus.SUCCEEDED
        assert release_pay.amount == 300.0
        assert release_pay.customer_id == payee.id

    def test_cannot_release_twice(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=100.0,
        )
        engine.release_escrow(escrow.id)
        with pytest.raises(ValueError, match="Cannot release"):
            engine.release_escrow(escrow.id)

    def test_release_nonexistent_returns_none(self, engine):
        assert engine.release_escrow("esc_nonexistent") is None


# ── Escrow: Refund ──────────────────────────────────────────────────────────

class TestEscrowRefund:
    def test_refund_escrow_credits_payer(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=500.0,
        )
        refunded = engine.refund_escrow(escrow.id, reason="Task not done")
        assert refunded.status == EscrowStatus.REFUNDED
        assert refunded.refunded_at is not None
        # Payer gets money back
        updated_payer = engine.get_customer(payer.id)
        assert updated_payer.balance == 10000.0  # Full refund

    def test_refund_escrow_does_not_credit_payee(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=500.0,
        )
        engine.refund_escrow(escrow.id)
        updated_payee = engine.get_customer(payee.id)
        assert updated_payee.balance == 0.0

    def test_cannot_refund_after_release(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=100.0,
        )
        engine.release_escrow(escrow.id)
        with pytest.raises(ValueError, match="Cannot refund"):
            engine.refund_escrow(escrow.id)

    def test_refund_nonexistent_returns_none(self, engine):
        assert engine.refund_escrow("esc_nonexistent") is None


# ── Escrow: Dispute ─────────────────────────────────────────────────────────

class TestEscrowDispute:
    def test_dispute_escrow(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=100.0,
        )
        disputed = engine.dispute_escrow(escrow.id, reason="Payer won't release")
        assert disputed.status == EscrowStatus.DISPUTED
        assert disputed.dispute_reason == "Payer won't release"

    def test_cannot_dispute_released(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=100.0,
        )
        engine.release_escrow(escrow.id)
        with pytest.raises(ValueError, match="Cannot dispute"):
            engine.dispute_escrow(escrow.id, reason="too late")


# ── Escrow: Auto-Expire ─────────────────────────────────────────────────────

class TestEscrowAutoExpire:
    def test_auto_expire_refunds_past_due(self, engine, two_customers):
        payer, payee = two_customers
        # Create escrow that already expired (1 second expiry, in the past)
        escrow = engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=200.0,
            expires_in_seconds=-1,  # Already expired
        )
        expired = engine.auto_expire_escrows()
        assert len(expired) == 1
        assert expired[0].status == EscrowStatus.REFUNDED
        # Payer refunded
        assert engine.get_customer(payer.id).balance == 10000.0

    def test_auto_expire_skips_non_expired(self, engine, two_customers):
        payer, payee = two_customers
        engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=200.0,
            expires_in_seconds=3600,  # 1 hour from now
        )
        expired = engine.auto_expire_escrows()
        assert len(expired) == 0

    def test_auto_expire_skips_no_expiry(self, engine, two_customers):
        payer, payee = two_customers
        engine.create_escrow(
            payer_customer_id=payer.id,
            payee_customer_id=payee.id,
            amount=200.0,
            # no expires_in_seconds
        )
        expired = engine.auto_expire_escrows()
        assert len(expired) == 0


# ── Escrow: Listing ─────────────────────────────────────────────────────────

class TestEscrowListing:
    def test_list_escrows_by_payer(self, engine, two_customers):
        payer, payee = two_customers
        e1 = engine.create_escrow(payer.id, payee.id, 100.0)
        e2 = engine.create_escrow(payer.id, payee.id, 200.0)
        result = engine.list_escrows(payer_id=payer.id)
        assert len(result) == 2

    def test_list_escrows_by_status(self, engine, two_customers):
        payer, payee = two_customers
        e1 = engine.create_escrow(payer.id, payee.id, 100.0)
        e2 = engine.create_escrow(payer.id, payee.id, 200.0)
        engine.release_escrow(e1.id)
        held = engine.list_escrows(status="held")
        released = engine.list_escrows(status="released")
        assert len(held) == 1
        assert len(released) == 1

    def test_list_escrows_by_payee(self, engine, two_customers):
        payer, payee = two_customers
        other = engine.create_customer(name="Other")
        e1 = engine.create_escrow(payer.id, payee.id, 100.0)
        e2 = engine.create_escrow(payer.id, other.id, 50.0)
        payee_escrows = engine.list_escrows(payee_id=payee.id)
        assert len(payee_escrows) == 1
        assert payee_escrows[0].payee_customer_id == payee.id

    def test_get_escrow(self, engine, two_customers):
        payer, payee = two_customers
        escrow = engine.create_escrow(payer.id, payee.id, 100.0)
        fetched = engine.get_escrow(escrow.id)
        assert fetched is not None
        assert fetched.id == escrow.id

    def test_get_nonexistent_escrow(self, engine):
        assert engine.get_escrow("esc_fake") is None


# ── Split Payments: Creation ────────────────────────────────────────────────

class TestSplitCreation:
    def test_create_split_with_fixed_amounts(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        b = engine.create_customer(name="B")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[
                {"customer_id": a.id, "amount": 7.00, "label": "provider"},
                {"customer_id": b.id, "amount": 3.00, "label": "platform"},
            ],
        )
        assert split.total_amount == 10.0
        assert len(split.shares) == 2
        assert split.status == SplitStatus.COMPLETED  # auto_settled

    def test_create_split_with_percentages(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        b = engine.create_customer(name="B")
        c = engine.create_customer(name="C")
        # Percentages without amounts or source payment should error
        with pytest.raises(ValueError, match="Cannot determine total"):
            engine.create_split(
                payer_customer_id=payer.id,
                shares=[
                    {"customer_id": a.id, "percentage": 70, "label": "provider"},
                    {"customer_id": b.id, "percentage": 20, "label": "platform"},
                    {"customer_id": c.id, "percentage": 10, "label": "referrer"},
                ],
            )

    def test_create_split_percentages_from_source_payment(self, engine):
        payer = engine.create_customer(name="Payer")
        engine.top_up_balance(payer.id, 10000.0)
        a = engine.create_customer(name="A")
        b = engine.create_customer(name="B")
        # Charge first to get a source payment
        payment = engine.charge(payer.id, 10.0)
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[
                {"customer_id": a.id, "percentage": 70, "label": "provider"},
                {"customer_id": b.id, "percentage": 30, "label": "platform"},
            ],
            source_payment_id=payment.id,
        )
        assert split.total_amount == 10.0
        assert split.shares[0].amount == 7.0
        assert split.shares[1].amount == 3.0

    def test_create_split_auto_settles(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="Recipient A")
        b = engine.create_customer(name="Recipient B")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[
                {"customer_id": a.id, "amount": 6.0},
                {"customer_id": b.id, "amount": 4.0},
            ],
        )
        # Recipients should be credited
        assert engine.get_customer(a.id).balance == 6.0
        assert engine.get_customer(b.id).balance == 4.0
        assert split.status == SplitStatus.COMPLETED
        assert len(split.settlement_payment_ids) == 2

    def test_create_split_no_auto_settle(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[{"customer_id": a.id, "amount": 5.0}],
            auto_settle=False,
        )
        assert split.status == SplitStatus.PENDING
        # Not credited yet
        assert engine.get_customer(a.id).balance == 0.0


class TestSplitSettlement:
    def test_settle_pending_split(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        b = engine.create_customer(name="B")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[
                {"customer_id": a.id, "amount": 6.0},
                {"customer_id": b.id, "amount": 4.0},
            ],
            auto_settle=False,
        )
        settled = engine.settle_split(split.id)
        assert settled.status == SplitStatus.COMPLETED
        assert engine.get_customer(a.id).balance == 6.0
        assert engine.get_customer(b.id).balance == 4.0

    def test_settle_idempotent(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[{"customer_id": a.id, "amount": 5.0}],
            auto_settle=False,
        )
        engine.settle_split(split.id)
        # Settle again — should not double-credit
        result = engine.settle_split(split.id)
        assert result.status == SplitStatus.COMPLETED
        assert engine.get_customer(a.id).balance == 5.0  # Still only 5, not 10

    def test_settle_with_unknown_recipient(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[
                {"customer_id": a.id, "amount": 5.0},
                {"customer_id": "cus_ghost", "amount": 5.0},
            ],
            auto_settle=False,
        )
        settled = engine.settle_split(split.id)
        assert settled.status == SplitStatus.PARTIALLY_COMPLETED
        # Only A got credited
        assert engine.get_customer(a.id).balance == 5.0


class TestSplitGetList:
    def test_get_split(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        split = engine.create_split(
            payer_customer_id=payer.id,
            shares=[{"customer_id": a.id, "amount": 5.0}],
        )
        fetched = engine.get_split(split.id)
        assert fetched is not None
        assert fetched.id == split.id

    def test_get_nonexistent_split(self, engine):
        assert engine.get_split("spl_fake") is None

    def test_list_splits_by_payer(self, engine):
        payer = engine.create_customer(name="Payer")
        other = engine.create_customer(name="Other")
        a = engine.create_customer(name="A")
        s1 = engine.create_split(payer.id, [{"customer_id": a.id, "amount": 5.0}])
        s2 = engine.create_split(other.id, [{"customer_id": a.id, "amount": 3.0}])
        result = engine.list_splits(payer_id=payer.id)
        assert len(result) == 1
        assert result[0].payer_customer_id == payer.id

    def test_list_splits_by_status(self, engine):
        payer = engine.create_customer(name="Payer")
        a = engine.create_customer(name="A")
        s1 = engine.create_split(payer.id, [{"customer_id": a.id, "amount": 5.0}])
        s2 = engine.create_split(
            payer.id, [{"customer_id": a.id, "amount": 3.0}], auto_settle=False
        )
        completed = engine.list_splits(status="completed")
        pending = engine.list_splits(status="pending")
        assert len(completed) == 1
        assert len(pending) == 1


# ── Persistence ─────────────────────────────────────────────────────────────

class TestEscrowSplitPersistence:
    def test_escrow_persists_across_storage_reload(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "p"))
        engine1 = PaymentEngine(storage=storage1)
        payer = engine1.create_customer(name="P")
        payee = engine1.create_customer(name="B")
        engine1.top_up_balance(payer.id, 1000.0)
        esc = engine1.create_escrow(payer.id, payee.id, 200.0, task_description="persist test")

        # Reload storage
        storage2 = Storage(data_dir=str(tmp_path / "p"))
        loaded = storage2.get_escrow(esc.id)
        assert loaded is not None
        assert loaded.amount == 200.0
        assert loaded.task_description == "persist test"

    def test_split_persists_across_storage_reload(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "p2"))
        engine1 = PaymentEngine(storage=storage1)
        payer = engine1.create_customer(name="P")
        a = engine1.create_customer(name="A")
        split = engine1.create_split(payer.id, [{"customer_id": a.id, "amount": 5.0}])

        storage2 = Storage(data_dir=str(tmp_path / "p2"))
        loaded = storage2.get_split(split.id)
        assert loaded is not None
        assert loaded.total_amount == 5.0
        assert len(loaded.shares) == 1


# ── MCP Server Integration ──────────────────────────────────────────────────

class TestMCPServerEscrowSplit:
    def test_server_creates_escrow(self, server, engine):
        payer = engine.create_customer(name="P")
        payee = engine.create_customer(name="B")
        engine.top_up_balance(payer.id, 1000.0)
        result = server.call_tool("create_escrow", {
            "payer_customer_id": payer.id,
            "payee_customer_id": payee.id,
            "amount": 300,
            "task_description": "Process data",
        })
        assert "result" in result
        assert result["result"]["status"] == "held"
        assert result["result"]["amount"] == 300

    def test_server_releases_escrow(self, server, engine):
        payer = engine.create_customer(name="P")
        payee = engine.create_customer(name="B")
        engine.top_up_balance(payer.id, 1000.0)
        esc = engine.create_escrow(payer.id, payee.id, 200.0)
        result = server.call_tool("release_escrow", {"escrow_id": esc.id})
        assert result["result"]["status"] == "released"

    def test_server_creates_split(self, server, engine):
        payer = engine.create_customer(name="P")
        a = engine.create_customer(name="A")
        b = engine.create_customer(name="B")
        result = server.call_tool("create_split", {
            "payer_customer_id": payer.id,
            "shares": [
                {"customer_id": a.id, "amount": 7, "label": "provider"},
                {"customer_id": b.id, "amount": 3, "label": "fee"},
            ],
        })
        assert result["result"]["status"] == "completed"
        assert result["result"]["total_amount"] == 10

    def test_server_list_escrows(self, server, engine):
        payer = engine.create_customer(name="P")
        payee = engine.create_customer(name="B")
        engine.top_up_balance(payer.id, 1000.0)
        engine.create_escrow(payer.id, payee.id, 100.0)
        engine.create_escrow(payer.id, payee.id, 200.0)
        result = server.call_tool("list_escrows", {"payer_id": payer.id})
        assert result["result"]["count"] == 2

    def test_server_get_escrow(self, server, engine):
        payer = engine.create_customer(name="P")
        payee = engine.create_customer(name="B")
        engine.top_up_balance(payer.id, 1000.0)
        esc = engine.create_escrow(payer.id, payee.id, 150.0)
        result = server.call_tool("get_escrow", {"escrow_id": esc.id})
        assert result["result"]["status"] == "held"
        assert result["result"]["amount"] == 150

    def test_server_get_split(self, server, engine):
        payer = engine.create_customer(name="P")
        a = engine.create_customer(name="A")
        split = engine.create_split(payer.id, [{"customer_id": a.id, "amount": 5.0}])
        result = server.call_tool("get_split", {"split_id": split.id})
        assert result["result"]["status"] == "completed"

    def test_server_escrow_not_found(self, server):
        result = server.call_tool("get_escrow", {"escrow_id": "esc_fake"})
        assert "error" in result["result"]

    def test_server_split_not_found(self, server):
        result = server.call_tool("get_split", {"split_id": "spl_fake"})
        assert "error" in result["result"]

    def test_new_tools_in_tool_list(self, server):
        tools = server.list_tools()
        names = {t["name"] for t in tools}
        assert "create_escrow" in names
        assert "release_escrow" in names
        assert "refund_escrow" in names
        assert "get_escrow" in names
        assert "list_escrows" in names
        assert "create_split" in names
        assert "get_split" in names
