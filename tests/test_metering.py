"""Tests for v0.4.0 Usage Metering — record, summarize, settle."""
import pytest
from datetime import datetime, timedelta, timezone

from mcp_payments.engine import PaymentEngine
from mcp_payments.models import (
    Currency,
    PaymentStatus,
    PricingModel,
    UsageUnit,
)
from mcp_payments.storage import Storage


@pytest.fixture
def engine(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def customer(engine):
    c = engine.create_customer(name="Metered Agent", agent_id="said:meter001")
    engine.top_up_balance(c.id, 100000)  # $1000 prepaid
    return c


# ── record_usage ────────────────────────────────────────────────────────────

class TestRecordUsage:
    def test_record_single_call(self, engine, customer):
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="search_web",
            unit=UsageUnit.CALLS,
            quantity=1,
        )
        assert ev.id.startswith("usage_")
        assert ev.customer_id == customer.id
        assert ev.tool_name == "search_web"
        assert ev.unit == UsageUnit.CALLS
        assert ev.quantity == 1
        assert ev.settled is False

    def test_record_multiple_calls(self, engine, customer):
        for i in range(5):
            engine.record_usage(
                customer_id=customer.id,
                tool_name="search_web",
                quantity=1,
            )
        events = engine.list_usage_events(customer_id=customer.id)
        assert len(events) == 5

    def test_record_token_usage_auto_quantity(self, engine, customer):
        """When unit=TOKENS and input/output given, quantity auto-computed."""
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="llm_complete",
            unit=UsageUnit.TOKENS,
            input_tokens=500,
            output_tokens=200,
        )
        assert ev.quantity == 700
        assert ev.input_tokens == 500
        assert ev.output_tokens == 200

    def test_record_token_usage_explicit_quantity(self, engine, customer):
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="llm_complete",
            unit=UsageUnit.TOKENS,
            quantity=1500,
        )
        assert ev.quantity == 1500

    def test_record_usage_unknown_customer(self, engine):
        with pytest.raises(ValueError, match="Customer not found"):
            engine.record_usage(
                customer_id="cus_nonexistent",
                tool_name="search",
            )

    def test_record_usage_with_metadata(self, engine, customer):
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="translate",
            metadata={"source": "en", "target": "es"},
        )
        assert ev.metadata["source"] == "en"

    def test_record_usage_with_session(self, engine, customer):
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="search",
            session_id="sess_abc123",
            request_id="req_001",
        )
        assert ev.session_id == "sess_abc123"
        assert ev.request_id == "req_001"

    def test_record_seconds_unit(self, engine, customer):
        ev = engine.record_usage(
            customer_id=customer.id,
            tool_name="compute",
            unit=UsageUnit.SECONDS,
            quantity=30,
        )
        assert ev.unit == UsageUnit.SECONDS
        assert ev.quantity == 30


# ── get_usage_summary ───────────────────────────────────────────────────────

class TestUsageSummary:
    def test_summary_empty(self, engine, customer):
        summary = engine.get_usage_summary(customer_id=customer.id)
        assert summary.total_events == 0
        assert summary.estimated_cost == 0
        assert summary.unsettled_events == 0

    def test_summary_with_events(self, engine, customer):
        for _ in range(10):
            engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)
        summary = engine.get_usage_summary(customer_id=customer.id)
        assert summary.total_events == 10
        assert summary.total_by_unit.get("calls") == 10
        assert summary.unsettled_events == 10

    def test_summary_with_pricing(self, engine, customer):
        """Summary estimates cost based on tool pricing."""
        engine.set_price(
            tool_name="search",
            amount=0.01,  # $0.01 per call
            pricing_model=PricingModel.PER_USE,
        )
        for _ in range(100):
            engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)
        summary = engine.get_usage_summary(customer_id=customer.id)
        assert summary.estimated_cost == pytest.approx(1.0)  # 100 * 0.01

    def test_summary_token_pricing(self, engine, customer):
        """Token-based pricing estimates cost per 1000 tokens."""
        engine.set_price(
            tool_name="llm_complete",
            amount=0.002,  # $0.002 per 1K tokens
            pricing_model=PricingModel.PER_TOKEN,
        )
        engine.record_usage(
            customer_id=customer.id,
            tool_name="llm_complete",
            unit=UsageUnit.TOKENS,
            quantity=5000,
        )
        summary = engine.get_usage_summary(customer_id=customer.id)
        # 5000 tokens / 1000 * $0.002 = $0.01
        assert summary.estimated_cost == pytest.approx(0.01)

    def test_summary_filtered_by_tool(self, engine, customer):
        engine.record_usage(customer_id=customer.id, tool_name="tool_a", quantity=1)
        engine.record_usage(customer_id=customer.id, tool_name="tool_b", quantity=1)
        summary = engine.get_usage_summary(customer_id=customer.id, tool_name="tool_a")
        assert summary.total_events == 1

    def test_summary_period_filter(self, engine, customer):
        """Events outside the period are excluded."""
        now = datetime.now(timezone.utc)
        # Record an event now
        engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)

        # Query a period in the future
        future_start = now + timedelta(days=10)
        future_end = now + timedelta(days=20)
        summary = engine.get_usage_summary(
            customer_id=customer.id,
            period_start=future_start,
            period_end=future_end,
        )
        assert summary.total_events == 0


# ── settle_usage ────────────────────────────────────────────────────────────

class TestSettleUsage:
    def test_settle_no_events(self, engine, customer):
        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 0
        assert result.total_charged == 0
        assert len(result.payment_ids) == 0

    def test_settle_charges_customer(self, engine, customer):
        """Settling metered usage creates a charge."""
        engine.set_price(
            tool_name="search",
            amount=0.05,
            pricing_model=PricingModel.PER_USE,
        )
        for _ in range(20):
            engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)

        balance_before = engine.get_customer(customer.id).balance
        result = engine.settle_usage(customer_id=customer.id)
        balance_after = engine.get_customer(customer.id).balance

        assert result.events_settled == 20
        assert result.total_charged == pytest.approx(1.0)  # 20 * 0.05
        assert len(result.payment_ids) == 1
        assert balance_before - balance_after == pytest.approx(1.0)

    def test_settle_marks_events_settled(self, engine, customer):
        engine.set_price("search", 0.01, pricing_model=PricingModel.PER_USE)
        engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)

        engine.settle_usage(customer_id=customer.id)

        events = engine.list_usage_events(customer_id=customer.id)
        assert all(e.settled for e in events)

    def test_settle_idempotent(self, engine, customer):
        """Settling twice doesn't double-charge."""
        engine.set_price("search", 0.01, pricing_model=PricingModel.PER_USE)
        engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)

        first = engine.settle_usage(customer_id=customer.id)
        second = engine.settle_usage(customer_id=customer.id)

        assert first.events_settled == 1
        assert second.events_settled == 0
        assert second.total_charged == 0

    def test_settle_multiple_tools(self, engine, customer):
        """Settling groups by tool and charges each separately."""
        engine.set_price("search", 0.01, pricing_model=PricingModel.PER_USE)
        engine.set_price("translate", 0.02, pricing_model=PricingModel.PER_USE)

        for _ in range(10):
            engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)
        for _ in range(5):
            engine.record_usage(customer_id=customer.id, tool_name="translate", quantity=1)

        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 15
        assert result.total_charged == pytest.approx(0.20)  # 10*0.01 + 5*0.02
        assert len(result.payment_ids) == 2
        assert "search" in result.breakdown
        assert "translate" in result.breakdown

    def test_settle_no_pricing_marks_settled_free(self, engine, customer):
        """Events without pricing are marked settled at $0."""
        engine.record_usage(customer_id=customer.id, tool_name="free_tool", quantity=1)
        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 1
        assert result.total_charged == 0
        assert result.breakdown["free_tool"]["charged"] is False

    def test_settle_specific_tool(self, engine, customer):
        """Settling with tool_name only settles that tool."""
        engine.set_price("search", 0.01, pricing_model=PricingModel.PER_USE)
        engine.set_price("translate", 0.02, pricing_model=PricingModel.PER_USE)

        engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)
        engine.record_usage(customer_id=customer.id, tool_name="translate", quantity=1)

        result = engine.settle_usage(customer_id=customer.id, tool_name="search")
        assert result.events_settled == 1
        assert "search" in result.breakdown
        assert "translate" not in result.breakdown

    def test_settle_token_based(self, engine, customer):
        """Token-based settlement charges per 1K tokens."""
        engine.set_price(
            "llm_complete",
            amount=0.005,  # $0.005 per 1K tokens
            pricing_model=PricingModel.PER_TOKEN,
        )
        engine.record_usage(
            customer_id=customer.id,
            tool_name="llm_complete",
            unit=UsageUnit.TOKENS,
            quantity=10000,
        )
        result = engine.settle_usage(customer_id=customer.id)
        # 10000 / 1000 * 0.005 = 0.05
        assert result.total_charged == pytest.approx(0.05)

    def test_settle_insufficient_balance(self, engine, customer):
        """Settlement charges even with insufficient balance (creates failed payment)."""
        engine.set_price("expensive", 200000, pricing_model=PricingModel.PER_USE)  # $2000 > $1000 balance
        engine.record_usage(customer_id=customer.id, tool_name="expensive", quantity=1)

        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 1
        # Payment should be failed due to insufficient balance
        assert result.breakdown["expensive"]["charged"] is False


# ── Storage persistence ──────────────────────────────────────────────────────

class TestUsageStoragePersistence:
    def test_usage_persists_across_restart(self, tmp_path):
        """Usage events survive storage reload."""
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        from mcp_payments.models import UsageEvent
        event = UsageEvent(
            customer_id="cus_test",
            tool_name="search",
            unit=UsageUnit.CALLS,
            quantity=5,
        )
        storage1.create_usage_event(event)

        # Reload
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        events = storage2.list_usage_events(customer_id="cus_test")
        assert len(events) == 1
        assert events[0].quantity == 5

    def test_mark_events_settled(self, tmp_path):
        storage = Storage(data_dir=str(tmp_path / "payments"))
        from mcp_payments.models import UsageEvent
        e1 = UsageEvent(customer_id="c1", tool_name="t", quantity=1)
        e2 = UsageEvent(customer_id="c1", tool_name="t", quantity=2)
        storage.create_usage_event(e1)
        storage.create_usage_event(e2)

        count = storage.mark_events_settled([e1.id, e2.id])
        assert count == 2

        events = storage.list_usage_events(customer_id="c1")
        assert all(e.settled for e in events)

    def test_delete_usage_event(self, tmp_path):
        storage = Storage(data_dir=str(tmp_path / "payments"))
        from mcp_payments.models import UsageEvent
        event = UsageEvent(customer_id="c1", tool_name="t", quantity=1)
        storage.create_usage_event(event)
        assert storage.delete_usage_event(event.id) is True
        assert storage.get_usage_event(event.id) is None
        assert storage.delete_usage_event(event.id) is False


# ── Integration: full metered billing cycle ──────────────────────────────────

class TestMeteredBillingCycle:
    def test_full_cycle_record_summarize_settle(self, engine, customer):
        """End-to-end: set pricing → record usage → summarize → settle."""
        # 1. Set pricing
        engine.set_price("api_call", 0.03, pricing_model=PricingModel.PER_USE)

        # 2. Record usage throughout the "month"
        for _ in range(50):
            engine.record_usage(customer_id=customer.id, tool_name="api_call", quantity=1)

        # 3. Check summary before settlement
        summary = engine.get_usage_summary(customer_id=customer.id)
        assert summary.total_events == 50
        assert summary.estimated_cost == pytest.approx(1.50)
        assert summary.unsettled_events == 50

        # 4. Settle
        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 50
        assert result.total_charged == pytest.approx(1.50)

        # 5. Verify summary after settlement
        summary_after = engine.get_usage_summary(customer_id=customer.id)
        assert summary_after.settled_events == 50
        assert summary_after.unsettled_events == 0

    def test_metered_with_mixed_units(self, engine, customer):
        """Mix of per-use and token-based tools."""
        engine.set_price("search", 0.01, pricing_model=PricingModel.PER_USE)
        engine.set_price("llm", 0.003, pricing_model=PricingModel.PER_TOKEN)

        # 100 search calls
        for _ in range(100):
            engine.record_usage(customer_id=customer.id, tool_name="search", quantity=1)
        # 5000 tokens
        engine.record_usage(
            customer_id=customer.id,
            tool_name="llm",
            unit=UsageUnit.TOKENS,
            quantity=5000,
        )

        summary = engine.get_usage_summary(customer_id=customer.id)
        # 100 * 0.01 + 5 * 0.003 = 1.015
        assert summary.estimated_cost == pytest.approx(1.015)

        result = engine.settle_usage(customer_id=customer.id)
        assert result.events_settled == 101
        assert result.total_charged == pytest.approx(1.02)  # rounded
