"""
Tests for v0.7.0 Dispute Resolution.

Covers:
- Dispute lifecycle: open → respond → escalate → resolve
- Evidence submission (both parties)
- Communication thread
- Resolution outcomes: release, refund, split
- Fund movements on each outcome
- Auto-expiry (no response → refund; unresolved → split)
- Cancel by filer (restores escrow to held)
- Only filer can cancel
- Duplicate dispute prevention
- Escrow status transitions during dispute
- Dispute statistics
- MCP server tool integration
- Edge cases: resolved disputes reject actions, party validation
"""

import pytest
from datetime import datetime, timezone, timedelta

from mcp_payments.models import (
    Currency,
    DisputeStatus,
    DisputeParty,
    EscrowStatus,
    PaymentStatus,
)
from mcp_payments.engine import PaymentEngine
from mcp_payments.storage import Storage
from mcp_payments.server import MCPServer


@pytest.fixture
def engine(tmp_path):
    """Fresh engine with temp storage."""
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def server(engine):
    return MCPServer(engine=engine)


@pytest.fixture
def two_customers(engine):
    """Create payer + payee with balances."""
    payer = engine.create_customer(name="Payer Agent", agent_id="said:payer")
    payee = engine.create_customer(name="Payee Agent", agent_id="said:payee")
    engine.top_up_balance(payer.id, 10000)
    engine.top_up_balance(payee.id, 5000)
    return payer, payee


@pytest.fixture
def escrow(engine, two_customers):
    """Create a funded escrow between two agents."""
    payer, payee = two_customers
    return engine.create_escrow(
        payer_customer_id=payer.id,
        payee_customer_id=payee.id,
        amount=500,
        task_description="Generate a market research report",
        tool_name="research-agent",
    )


@pytest.fixture
def arbiter(engine):
    """Create an arbiter customer."""
    return engine.create_customer(name="Arbiter", agent_id="said:arbiter")


# ── Dispute Opening ───────────────────────────────────────────────────

class TestOpenDispute:
    def test_open_dispute_by_payer(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="Task was not completed",
            category="non_delivery",
        )
        assert dispute.id is not None
        assert dispute.status == DisputeStatus.OPENED
        assert dispute.filed_by == DisputeParty.PAYER
        assert dispute.payer_customer_id == payer.id
        assert dispute.payee_customer_id == payee.id
        assert dispute.amount == 500
        assert dispute.reason == "Task was not completed"
        assert dispute.category == "non_delivery"
        assert len(dispute.messages) == 1
        assert dispute.messages[0].system is True
        assert dispute.expires_at is not None

    def test_open_dispute_by_payee(self, engine, escrow):
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payee",
            reason="Funds not released after delivery",
            category="non_payment",
        )
        assert dispute.filed_by == DisputeParty.PAYEE
        assert dispute.status == DisputeStatus.OPENED

    def test_open_dispute_marks_escrow_disputed(self, engine, escrow):
        engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="Bad quality",
        )
        updated = engine.get_escrow(escrow.id)
        assert updated.status == EscrowStatus.DISPUTED
        assert updated.dispute_reason == "Bad quality"

    def test_open_dispute_no_expiry(self, engine, escrow):
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="Issue",
            expires_in_seconds=None,
        )
        assert dispute.expires_at is None

    def test_open_dispute_nonexistent_escrow(self, engine):
        with pytest.raises(ValueError, match="Escrow not found"):
            engine.open_dispute(
                escrow_id="esc_nonexistent",
                filed_by="payer",
                reason="test",
            )

    def test_open_dispute_wrong_escrow_status(self, engine, escrow):
        # Release the escrow first
        engine.release_escrow(escrow.id)
        with pytest.raises(ValueError, match="Cannot dispute"):
            engine.open_dispute(
                escrow_id=escrow.id,
                filed_by="payer",
                reason="test",
            )

    def test_open_duplicate_dispute_fails(self, engine, escrow):
        engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="First dispute",
        )
        with pytest.raises(ValueError, match="already has an active dispute"):
            engine.open_dispute(
                escrow_id=escrow.id,
                filed_by="payee",
                reason="Second dispute",
            )

    def test_open_dispute_after_resolved_allows_new(self, engine, escrow, arbiter):
        d1 = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="First")
        engine.resolve_dispute(d1.id, outcome="refund", resolved_by=arbiter.id)
        # Escrow is now refunded — re-create fresh scenario
        payer = engine.get_customer(escrow.payer_customer_id)
        engine.top_up_balance(payer.id, 500)
        esc2 = engine.create_escrow(
            payer_customer_id=escrow.payer_customer_id,
            payee_customer_id=escrow.payee_customer_id,
            amount=500,
        )
        d2 = engine.open_dispute(escrow_id=esc2.id, filed_by="payer", reason="Second")
        assert d2.status == DisputeStatus.OPENED


# ── Evidence Submission ───────────────────────────────────────────────

class TestEvidence:
    def test_submit_evidence_filer(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        evd = engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payer.id,
            content="The agent never delivered the report",
            evidence_url="https://example.com/proof.png",
            evidence_type="screenshot",
        )
        assert evd.id is not None
        assert evd.party == DisputeParty.PAYER
        assert evd.content == "The agent never delivered the report"
        assert evd.evidence_url == "https://example.com/proof.png"
        assert evd.evidence_type == "screenshot"

        # Verify it's stored
        updated = engine.get_dispute(dispute.id)
        assert len(updated.evidence) == 1

    def test_submit_evidence_counterparty_transitions_status(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        assert dispute.status == DisputeStatus.OPENED

        # Payee responds with evidence
        engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payee.id,
            content="I delivered the report, here's the proof",
            evidence_type="receipt",
        )
        updated = engine.get_dispute(dispute.id)
        assert updated.status == DisputeStatus.RESPONDED
        assert updated.responded_at is not None

    def test_submit_evidence_filer_does_not_transition(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        # Same party submits — should stay OPENED
        engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payer.id,
            content="More evidence",
        )
        updated = engine.get_dispute(dispute.id)
        assert updated.status == DisputeStatus.OPENED

    def test_submit_evidence_non_party_rejected(self, engine, escrow):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="not a party"):
            engine.submit_evidence(
                dispute_id=dispute.id,
                submitted_by="cus_unknown",
                content="test",
            )

    def test_submit_evidence_resolved_rejected(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.resolve_dispute(dispute.id, outcome="refund", resolved_by=arbiter.id)
        with pytest.raises(ValueError, match="Cannot submit evidence"):
            engine.submit_evidence(
                dispute_id=dispute.id,
                submitted_by=escrow.payer_customer_id,
                content="late evidence",
            )


# ── Communication Thread ──────────────────────────────────────────────

class TestMessages:
    def test_send_message(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        msg = engine.send_dispute_message(
            dispute_id=dispute.id,
            from_customer_id=payee.id,
            message="I completed the task, please check your inbox",
        )
        assert msg.id is not None
        assert msg.party == DisputeParty.PAYEE
        assert msg.message == "I completed the task, please check your inbox"
        assert msg.system is False

    def test_message_counterparty_transitions_status(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.send_dispute_message(
            dispute_id=dispute.id,
            from_customer_id=payee.id,
            message="Response",
        )
        updated = engine.get_dispute(dispute.id)
        assert updated.status == DisputeStatus.RESPONDED

    def test_message_non_party_rejected(self, engine, escrow):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="not a party"):
            engine.send_dispute_message(
                dispute_id=dispute.id,
                from_customer_id="cus_unknown",
                message="test",
            )


# ── Escalation ────────────────────────────────────────────────────────

class TestEscalation:
    def test_escalate_opened(self, engine, escrow):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        escalated = engine.escalate_dispute(dispute.id)
        assert escalated.status == DisputeStatus.UNDER_REVIEW

    def test_escalate_responded(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.send_dispute_message(dispute.id, from_customer_id=payee.id, message="hi")
        escalated = engine.escalate_dispute(dispute.id)
        assert escalated.status == DisputeStatus.UNDER_REVIEW

    def test_escalate_resolved_fails(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.resolve_dispute(dispute.id, outcome="refund", resolved_by=arbiter.id)
        with pytest.raises(ValueError, match="Cannot escalate"):
            engine.escalate_dispute(dispute.id)


# ── Resolution ────────────────────────────────────────────────────────

class TestResolution:
    def test_resolve_release(self, engine, escrow, two_customers, arbiter):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")

        payer_before = engine.get_customer(payer.id).balance
        payee_before = engine.get_customer(payee.id).balance

        result = engine.resolve_dispute(
            dispute_id=dispute.id,
            outcome="release",
            resolved_by=arbiter.id,
            resolution="Payee proved delivery",
        )
        assert result.status == DisputeStatus.RESOLVED_RELEASE
        assert result.resolved_at is not None
        assert result.resolved_by == arbiter.id
        assert result.resolution == "Payee proved delivery"

        # Funds credited to payee
        payee_after = engine.get_customer(payee.id).balance
        assert payee_after == payee_before + 500
        # Payer unchanged (already debited at escrow creation)
        payer_after = engine.get_customer(payer.id).balance
        assert payer_after == payer_before

        # Escrow released
        esc = engine.get_escrow(escrow.id)
        assert esc.status == EscrowStatus.RELEASED
        assert esc.released_at is not None

    def test_resolve_refund(self, engine, escrow, two_customers, arbiter):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")

        payer_before = engine.get_customer(payer.id).balance
        payee_before = engine.get_customer(payee.id).balance

        result = engine.resolve_dispute(
            dispute_id=dispute.id,
            outcome="refund",
            resolved_by=arbiter.id,
        )
        assert result.status == DisputeStatus.RESOLVED_REFUND

        # Funds returned to payer
        payer_after = engine.get_customer(payer.id).balance
        assert payer_after == payer_before + 500
        # Payee unchanged
        payee_after = engine.get_customer(payee.id).balance
        assert payee_after == payee_before

        # Escrow refunded
        esc = engine.get_escrow(escrow.id)
        assert esc.status == EscrowStatus.REFUNDED

    def test_resolve_split(self, engine, escrow, two_customers, arbiter):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")

        payer_before = engine.get_customer(payer.id).balance
        payee_before = engine.get_customer(payee.id).balance

        result = engine.resolve_dispute(
            dispute_id=dispute.id,
            outcome="split",
            resolved_by=arbiter.id,
            split_percentage=60,  # 60% to payer, 40% to payee
            resolution="Partial delivery — 60% refund",
        )
        assert result.status == DisputeStatus.RESOLVED_SPLIT
        assert result.split_percentage == 60

        # Payer gets 60% = 300
        payer_after = engine.get_customer(payer.id).balance
        assert payer_after == payer_before + 300
        # Payee gets 40% = 200
        payee_after = engine.get_customer(payee.id).balance
        assert payee_after == payee_before + 200

    def test_resolve_split_requires_percentage(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="requires split_percentage"):
            engine.resolve_dispute(dispute.id, outcome="split", resolved_by=arbiter.id)

    def test_resolve_split_invalid_percentage(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="between 0 and 100"):
            engine.resolve_dispute(
                dispute.id, outcome="split", resolved_by=arbiter.id, split_percentage=150
            )

    def test_resolve_unknown_outcome(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="Unknown outcome"):
            engine.resolve_dispute(dispute.id, outcome="freeze", resolved_by=arbiter.id)

    def test_resolve_already_resolved(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.resolve_dispute(dispute.id, outcome="refund", resolved_by=arbiter.id)
        with pytest.raises(ValueError, match="already resolved"):
            engine.resolve_dispute(dispute.id, outcome="release", resolved_by=arbiter.id)

    def test_resolve_records_payment(self, engine, escrow, two_customers, arbiter):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.resolve_dispute(dispute.id, outcome="release", resolved_by=arbiter.id)

        # Check a payment was recorded
        payments = engine.storage.list_payments(customer_id=payee.id, limit=10000)
        dispute_payments = [p for p in payments if p.metadata.get("dispute_id") == dispute.id]
        assert len(dispute_payments) == 1
        assert dispute_payments[0].amount == 500
        assert dispute_payments[0].status == PaymentStatus.SUCCEEDED


# ── Cancellation ──────────────────────────────────────────────────────

class TestCancellation:
    def test_cancel_by_filer(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        result = engine.cancel_dispute(dispute.id, by_customer_id=payer.id, reason="Resolved directly")
        assert result.status == DisputeStatus.CANCELLED
        assert "Resolved directly" in result.resolution

        # Escrow restored to HELD
        esc = engine.get_escrow(escrow.id)
        assert esc.status == EscrowStatus.HELD
        assert esc.dispute_reason is None

    def test_cancel_by_non_filer_rejected(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        with pytest.raises(ValueError, match="Only the filing party"):
            engine.cancel_dispute(dispute.id, by_customer_id=payee.id)

    def test_cancel_resolved_rejected(self, engine, escrow, arbiter):
        dispute = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        engine.resolve_dispute(dispute.id, outcome="refund", resolved_by=arbiter.id)
        with pytest.raises(ValueError, match="Cannot cancel"):
            engine.cancel_dispute(dispute.id, by_customer_id=escrow.payer_customer_id)


# ── Auto-Expiry ────────────────────────────────────────────────────────

class TestAutoExpiry:
    def test_auto_expire_no_response_refunds(self, engine, escrow, two_customers):
        payer, payee = two_customers
        # Create dispute with short expiry
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="test",
            expires_in_seconds=1,
        )
        # Manually set expiry to past
        engine.storage.update_dispute(dispute.id, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

        expired = engine.auto_expire_disputes()
        assert len(expired) == 1
        assert expired[0].status == DisputeStatus.RESOLVED_REFUND

        # Payer got money back
        payer_bal = engine.get_customer(payer.id).balance
        assert payer_bal == 10000  # original top-up

    def test_auto_expire_unresolved_splits(self, engine, escrow, two_customers):
        payer, payee = two_customers
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="test",
            expires_in_seconds=1,
        )
        # Counterparty responds (moves to RESPONDED)
        engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payee.id,
            content="I did the work",
        )
        # Set expiry to past
        engine.storage.update_dispute(dispute.id, expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

        expired = engine.auto_expire_disputes()
        assert len(expired) == 1
        assert expired[0].status == DisputeStatus.RESOLVED_SPLIT

        # 50/50 split: payer gets 250, payee gets 250
        payer_bal = engine.get_customer(payer.id).balance
        payee_bal = engine.get_customer(payee.id).balance
        assert payer_bal == 9750  # 10000 - 500 + 250
        assert payee_bal == 5250  # 5000 + 250


# ── Listing & Filtering ───────────────────────────────────────────────

class TestListing:
    def test_list_by_customer(self, engine, escrow, two_customers):
        payer, payee = two_customers
        engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")

        # Both parties should see it
        payer_disputes = engine.list_disputes(customer_id=payer.id)
        payee_disputes = engine.list_disputes(customer_id=payee.id)
        assert len(payer_disputes) == 1
        assert len(payee_disputes) == 1

    def test_list_by_status(self, engine, escrow):
        engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        opened = engine.list_disputes(status="opened")
        assert len(opened) == 1
        resolved = engine.list_disputes(status="resolved_refund")
        assert len(resolved) == 0

    def test_list_by_escrow(self, engine, escrow):
        engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test")
        results = engine.list_disputes(escrow_id=escrow.id)
        assert len(results) == 1
        assert results[0].escrow_id == escrow.id


# ── Statistics ────────────────────────────────────────────────────────

class TestStats:
    def test_dispute_stats_empty(self, engine):
        stats = engine.get_dispute_stats()
        assert stats["total_disputes"] == 0
        assert stats["resolution_rate"] == 0.0

    def test_dispute_stats_with_data(self, engine, escrow, arbiter):
        d1 = engine.open_dispute(escrow_id=escrow.id, filed_by="payer", reason="test", category="non_delivery")
        engine.resolve_dispute(d1.id, outcome="refund", resolved_by=arbiter.id)

        # Create second escrow for another dispute
        payer_id = escrow.payer_customer_id
        payee_id = escrow.payee_customer_id
        engine.top_up_balance(payer_id, 500)
        esc2 = engine.create_escrow(
            payer_customer_id=payer_id,
            payee_customer_id=payee_id,
            amount=300,
        )
        d2 = engine.open_dispute(escrow_id=esc2.id, filed_by="payee", reason="no release", category="non_payment")

        stats = engine.get_dispute_stats()
        assert stats["total_disputes"] == 2
        assert stats["resolved"] == 1
        assert stats["active"] == 1
        assert stats["resolution_rate"] == 50.0
        assert stats["total_disputed_amount"] == 800
        assert "non_delivery" in stats["by_category"]
        assert "non_payment" in stats["by_category"]
        assert stats["by_outcome"].get("resolved_refund") == 1


# ── MCP Server Integration ────────────────────────────────────────────

class TestMCPServer:
    def test_server_open_dispute(self, server, escrow):
        result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "Not delivered",
            "category": "non_delivery",
        })
        assert "result" in result
        r = result["result"]
        assert r["status"] == "opened"
        assert r["filed_by"] == "payer"
        assert "dispute_id" in r

    def test_server_submit_evidence(self, server, escrow, two_customers):
        payer, payee = two_customers
        open_result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        dispute_id = open_result["result"]["dispute_id"]

        evd_result = server.call_tool("submit_evidence", {
            "dispute_id": dispute_id,
            "submitted_by": payer.id,
            "content": "Proof of non-delivery",
            "evidence_type": "screenshot",
        })
        assert "result" in evd_result
        assert "evidence_id" in evd_result["result"]

    def test_server_get_dispute(self, server, escrow):
        open_result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        dispute_id = open_result["result"]["dispute_id"]

        get_result = server.call_tool("get_dispute", {"dispute_id": dispute_id})
        r = get_result["result"]
        assert r["dispute_id"] == dispute_id
        assert r["status"] == "opened"
        assert len(r["messages"]) == 1

    def test_server_resolve_dispute(self, server, escrow, arbiter):
        open_result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        dispute_id = open_result["result"]["dispute_id"]

        resolve_result = server.call_tool("resolve_dispute", {
            "dispute_id": dispute_id,
            "outcome": "release",
            "resolved_by": arbiter.id,
            "resolution": "Payee delivered",
        })
        r = resolve_result["result"]
        assert r["status"] == "resolved_release"
        assert r["outcome"] == "release"

    def test_server_list_disputes(self, server, escrow):
        server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        list_result = server.call_tool("list_disputes", {})
        r = list_result["result"]
        assert r["count"] == 1

    def test_server_dispute_stats(self, server, escrow):
        server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        stats_result = server.call_tool("get_dispute_stats", {})
        r = stats_result["result"]
        assert r["total_disputes"] == 1

    def test_server_send_message(self, server, escrow, two_customers):
        payer, payee = two_customers
        open_result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        dispute_id = open_result["result"]["dispute_id"]

        msg_result = server.call_tool("send_dispute_message", {
            "dispute_id": dispute_id,
            "from_customer_id": payee.id,
            "message": "I delivered the work",
        })
        assert "result" in msg_result
        assert "message_id" in msg_result["result"]

    def test_server_cancel_dispute(self, server, escrow, two_customers):
        payer, payee = two_customers
        open_result = server.call_tool("open_dispute", {
            "escrow_id": escrow.id,
            "filed_by": "payer",
            "reason": "test",
        })
        dispute_id = open_result["result"]["dispute_id"]

        cancel_result = server.call_tool("cancel_dispute", {
            "dispute_id": dispute_id,
            "by_customer_id": payer.id,
            "reason": "Resolved amicably",
        })
        r = cancel_result["result"]
        assert r["status"] == "cancelled"

    def test_server_unknown_dispute(self, server):
        result = server.call_tool("get_dispute", {"dispute_id": "dsp_unknown"})
        assert "error" in result["result"]

    def test_tool_count_includes_disputes(self, server):
        tools = server.list_tools()
        tool_names = [t["name"] for t in tools]
        dispute_tools = [
            n for n in tool_names
            if n in {
                "open_dispute", "get_dispute", "list_disputes", "submit_evidence",
                "send_dispute_message", "escalate_dispute", "resolve_dispute",
                "cancel_dispute", "get_dispute_stats",
            }
        ]
        assert len(dispute_tools) == 9, f"Expected 9 dispute tools, got {dispute_tools}"


# ── Full Lifecycle Integration ────────────────────────────────────────

class TestFullLifecycle:
    def test_complete_dispute_lifecycle(self, engine, escrow, two_customers, arbiter):
        """End-to-end: open → evidence → message → escalate → resolve."""
        payer, payee = two_customers

        # 1. Payer opens dispute
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="Report quality was unacceptable",
            category="poor_quality",
        )
        assert dispute.status == DisputeStatus.OPENED

        # 2. Payer submits evidence
        engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payer.id,
            content="The report was only 2 pages and had no data",
            evidence_url="https://example.com/report.pdf",
            evidence_type="receipt",
        )

        # 3. Payee responds with their evidence
        engine.submit_evidence(
            dispute_id=dispute.id,
            submitted_by=payee.id,
            content="The report met all specs — 15 pages with full citations",
            evidence_url="https://example.com/full-report.pdf",
            evidence_type="receipt",
        )
        updated = engine.get_dispute(dispute.id)
        assert updated.status == DisputeStatus.RESPONDED

        # 4. Communication
        engine.send_dispute_message(
            dispute_id=dispute.id,
            from_customer_id=payer.id,
            message="The citations were broken links",
        )
        engine.send_dispute_message(
            dispute_id=dispute.id,
            from_customer_id=payee.id,
            message="I'll fix the citations, but the core work is done",
        )

        # 5. Escalate
        engine.escalate_dispute(dispute.id)
        updated = engine.get_dispute(dispute.id)
        assert updated.status == DisputeStatus.UNDER_REVIEW

        # 6. Arbiter resolves: split (partial quality)
        result = engine.resolve_dispute(
            dispute_id=dispute.id,
            outcome="split",
            resolved_by=arbiter.id,
            split_percentage=30,  # 30% back to payer
            resolution="Partial quality issues — 30% refund to payer, 70% to payee",
        )
        assert result.status == DisputeStatus.RESOLVED_SPLIT

        # Verify fund movements
        payer_final = engine.get_customer(payer.id).balance
        payee_final = engine.get_customer(payee.id).balance
        # Payer: 10000 - 500 (escrow) + 150 (30% of 500) = 9650
        assert payer_final == 9650
        # Payee: 5000 + 350 (70% of 500) = 5350
        assert payee_final == 5350

        # Verify the dispute has evidence and messages
        final = engine.get_dispute(dispute.id)
        assert len(final.evidence) == 2
        assert len(final.messages) >= 4  # initial + 2 messages + escalate + resolve

    def test_quick_resolution_refund(self, engine, escrow, two_customers, arbiter):
        """Payer disputes, counterparty doesn't respond, refunded."""
        payer, _ = two_customers
        dispute = engine.open_dispute(
            escrow_id=escrow.id,
            filed_by="payer",
            reason="Never delivered",
        )
        # No response — resolve directly
        result = engine.resolve_dispute(
            dispute_id=dispute.id,
            outcome="refund",
            resolved_by=arbiter.id,
            resolution="No response from payee",
        )
        assert result.status == DisputeStatus.RESOLVED_REFUND
        # Payer got full refund
        assert engine.get_customer(payer.id).balance == 10000
