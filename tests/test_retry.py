"""
Tests for v0.8.0 Payment Retry & Auto-Recovery.

Covers:
- RetryConfig CRUD (create, get, list, update, delete)
- Config resolution (customer-specific > global default)
- Failure classification (insufficient balance, timeout, rate limit, etc.)
- Delay computation (fixed, exponential, linear strategies + max cap)
- Retry trigger matching (configured triggers vs. failure type)
- PaymentRetry lifecycle: active → succeeded
- PaymentRetry lifecycle: active → exhausted
- Auto-retry creation on charge failure
- charge() integration: failed charges with retry config get retry_id
- Auto top-up before retry
- Cancel / pause / resume retry
- process_pending_retries (auto-recovery sweep)
- Retry stats aggregation
- MCP server integration (all 7 new tools)
- End-to-end: charge fails → retry → top-up → retry succeeds
"""

import pytest
from datetime import datetime, timezone, timedelta

from mcp_payments.models import (
    Currency,
    PaymentProvider,
    PaymentRetry,
    PaymentStatus,
    RetryAttempt,
    RetryConfig,
    RetryStatus,
    RetryStrategy,
    RetryTrigger,
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
def customer(engine):
    return engine.create_customer(name="Test Agent", email="agent@test.com")


@pytest.fixture
def funded_customer(engine):
    """Customer with balance."""
    c = engine.create_customer(name="Funded Agent", email="funded@test.com")
    engine.top_up_balance(c.id, 10000)  # $100
    return c


# ── RetryConfig CRUD ──────────────────────────────────────────────────

class TestRetryConfigCRUD:
    def test_configure_global_default(self, engine):
        config = engine.configure_retry(
            customer_id=None,
            name="global-default",
            max_attempts=5,
            strategy=RetryStrategy.EXPONENTIAL,
        )
        assert config.id is not None
        assert config.customer_id is None
        assert config.name == "global-default"
        assert config.max_attempts == 5
        assert config.strategy == RetryStrategy.EXPONENTIAL
        assert config.enabled is True

    def test_configure_customer_specific(self, engine, customer):
        config = engine.configure_retry(
            customer_id=customer.id,
            name="customer-policy",
            max_attempts=10,
        )
        assert config.customer_id == customer.id
        assert config.max_attempts == 10

    def test_configure_update_existing(self, engine, customer):
        """Configuring with same customer_id + name updates, not duplicates."""
        config1 = engine.configure_retry(customer_id=customer.id, name="default", max_attempts=3)
        config2 = engine.configure_retry(customer_id=customer.id, name="default", max_attempts=7)
        assert config1.id == config2.id
        assert config2.max_attempts == 7

    def test_get_retry_config(self, engine, customer):
        config = engine.configure_retry(customer_id=customer.id, name="test")
        fetched = engine.get_retry_config(config.id)
        assert fetched is not None
        assert fetched.id == config.id

    def test_list_retry_configs_all(self, engine, customer):
        engine.configure_retry(customer_id=None, name="global")
        engine.configure_retry(customer_id=customer.id, name="specific")
        configs = engine.list_retry_configs()
        assert len(configs) == 2

    def test_list_retry_configs_by_customer(self, engine, customer):
        engine.configure_retry(customer_id=None, name="global")
        engine.configure_retry(customer_id=customer.id, name="specific")
        configs = engine.list_retry_configs(customer_id=customer.id)
        assert len(configs) == 1
        assert configs[0].customer_id == customer.id

    def test_list_retry_configs_enabled_filter(self, engine, customer):
        engine.configure_retry(customer_id=customer.id, name="active", enabled=True)
        engine.configure_retry(customer_id=customer.id, name="inactive", enabled=False)
        enabled = engine.list_retry_configs(enabled=True)
        disabled = engine.list_retry_configs(enabled=False)
        assert len(enabled) == 1
        assert len(disabled) == 1

    def test_delete_retry_config(self, engine, customer):
        config = engine.configure_retry(customer_id=customer.id, name="temp")
        assert engine.delete_retry_config(config.id) is True
        assert engine.get_retry_config(config.id) is None
        assert engine.delete_retry_config(config.id) is False


# ── Config Resolution ─────────────────────────────────────────────────

class TestConfigResolution:
    def test_customer_specific_overrides_global(self, engine, customer):
        global_cfg = engine.configure_retry(
            customer_id=None, name="global", max_attempts=2
        )
        customer_cfg = engine.configure_retry(
            customer_id=customer.id, name="specific", max_attempts=10
        )
        resolved = engine.storage.get_retry_config_for_customer(customer.id)
        assert resolved.id == customer_cfg.id
        assert resolved.max_attempts == 10

    def test_global_fallback_when_no_customer_config(self, engine, customer):
        engine.configure_retry(customer_id=None, name="global", max_attempts=5)
        resolved = engine.storage.get_retry_config_for_customer(customer.id)
        assert resolved is not None
        assert resolved.customer_id is None

    def test_no_config_returns_none(self, engine, customer):
        resolved = engine.storage.get_retry_config_for_customer(customer.id)
        assert resolved is None

    def test_disabled_config_is_skipped(self, engine, customer):
        engine.configure_retry(customer_id=customer.id, name="disabled", enabled=False)
        resolved = engine.storage.get_retry_config_for_customer(customer.id)
        assert resolved is None


# ── Failure Classification ────────────────────────────────────────────

class TestFailureClassification:
    def test_classify_insufficient_balance(self, engine):
        trigger = engine.classify_failure("Insufficient balance: 5.00 < 10.00")
        assert trigger == RetryTrigger.INSUFFICIENT_BALANCE

    def test_classify_timeout(self, engine):
        trigger = engine.classify_failure("Request timeout to provider")
        assert trigger == RetryTrigger.PROVIDER_TIMEOUT

    def test_classify_timed_out(self, engine):
        trigger = engine.classify_failure("Connection timed out after 30s")
        assert trigger == RetryTrigger.PROVIDER_TIMEOUT

    def test_classify_rate_limited(self, engine):
        trigger = engine.classify_failure("Rate limit exceeded: 429")
        assert trigger == RetryTrigger.RATE_LIMITED

    def test_classify_provider_error_503(self, engine):
        trigger = engine.classify_failure("Provider returned 503 Service Unavailable")
        assert trigger == RetryTrigger.PROVIDER_ERROR

    def test_classify_network_error(self, engine):
        trigger = engine.classify_failure("Network connection refused")
        assert trigger == RetryTrigger.TRANSIENT_FAILURE

    def test_classify_unknown_error(self, engine):
        trigger = engine.classify_failure("Something completely unexpected happened")
        assert trigger == RetryTrigger.ALL_FAILURES

    def test_is_retryable_matching_trigger(self, engine, customer):
        config = engine.configure_retry(
            customer_id=customer.id,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
        )
        is_retryable, trigger = engine._is_retryable("Insufficient funds", config)
        assert is_retryable is True
        assert trigger == RetryTrigger.INSUFFICIENT_BALANCE

    def test_is_retryable_non_matching_trigger(self, engine, customer):
        config = engine.configure_retry(
            customer_id=customer.id,
            triggers=[RetryTrigger.PROVIDER_TIMEOUT],
        )
        is_retryable, _ = engine._is_retryable("Insufficient balance", config)
        assert is_retryable is False

    def test_is_retryable_all_failures_catchall(self, engine, customer):
        config = engine.configure_retry(
            customer_id=customer.id,
            triggers=[RetryTrigger.ALL_FAILURES],
        )
        is_retryable, _ = engine._is_retryable("Any random error", config)
        assert is_retryable is True


# ── Delay Computation ─────────────────────────────────────────────────

class TestDelayComputation:
    def test_fixed_strategy(self, engine):
        config = engine.configure_retry(
            strategy=RetryStrategy.FIXED,
            initial_delay_seconds=10.0,
            max_delay_seconds=100.0,
            customer_id="test_customer",
        )
        assert engine._compute_delay(1, config) == 10.0
        assert engine._compute_delay(5, config) == 10.0

    def test_exponential_strategy(self, engine):
        config = engine.configure_retry(
            strategy=RetryStrategy.EXPONENTIAL,
            initial_delay_seconds=2.0,
            backoff_multiplier=2.0,
            max_delay_seconds=1000.0,
            customer_id="test_customer",
        )
        assert engine._compute_delay(1, config) == 2.0   # 2 * 2^0
        assert engine._compute_delay(2, config) == 4.0   # 2 * 2^1
        assert engine._compute_delay(3, config) == 8.0   # 2 * 2^2
        assert engine._compute_delay(4, config) == 16.0  # 2 * 2^3

    def test_linear_strategy(self, engine):
        config = engine.configure_retry(
            strategy=RetryStrategy.LINEAR,
            initial_delay_seconds=5.0,
            max_delay_seconds=1000.0,
            customer_id="test_customer",
        )
        assert engine._compute_delay(1, config) == 5.0   # 5 * 1
        assert engine._compute_delay(2, config) == 10.0  # 5 * 2
        assert engine._compute_delay(3, config) == 15.0  # 5 * 3

    def test_max_delay_cap(self, engine):
        config = engine.configure_retry(
            strategy=RetryStrategy.EXPONENTIAL,
            initial_delay_seconds=10.0,
            backoff_multiplier=10.0,
            max_delay_seconds=50.0,
            customer_id="test_customer",
        )
        assert engine._compute_delay(1, config) == 10.0
        assert engine._compute_delay(2, config) == 50.0  # 100 capped to 50
        assert engine._compute_delay(3, config) == 50.0  # capped


# ── Retry Lifecycle ───────────────────────────────────────────────────

class TestRetryLifecycle:
    def test_retry_created_on_charge_failure(self, engine, customer):
        """When retry config exists and charge fails, a retry is auto-created."""
        engine.configure_retry(
            customer_id=None,  # global
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
        )
        # Customer has 0 balance — charge will fail
        payment = engine.charge(
            customer_id=customer.id,
            amount=1000,  # $10 — no balance
        )
        assert payment.status == PaymentStatus.FAILED
        assert "retry_id" in payment.metadata

        retry = engine.get_retry(payment.metadata["retry_id"])
        assert retry is not None
        assert retry.status == RetryStatus.ACTIVE
        assert retry.customer_id == customer.id
        assert retry.amount == 1000
        assert retry.trigger == RetryTrigger.INSUFFICIENT_BALANCE

    def test_no_retry_when_no_config(self, engine, customer):
        """Without a retry config, failed charges don't create retries."""
        payment = engine.charge(customer_id=customer.id, amount=1000)
        assert payment.status == PaymentStatus.FAILED
        assert "retry_id" not in payment.metadata

    def test_retry_not_created_for_non_retryable_failure(self, engine, customer):
        """If the failure type isn't in triggers, no retry is created."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.PROVIDER_TIMEOUT],  # Only timeout
        )
        # Insufficient balance failure doesn't match timeout trigger
        payment = engine.charge(customer_id=customer.id, amount=1000)
        assert payment.status == PaymentStatus.FAILED
        assert "retry_id" not in payment.metadata

    def test_retry_succeeds_on_execute(self, engine, customer):
        """Retry succeeds when balance is topped up before execution."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            max_attempts=3,
        )
        # Charge fails
        payment = engine.charge(customer_id=customer.id, amount=5000)
        assert payment.status == PaymentStatus.FAILED
        retry_id = payment.metadata["retry_id"]

        # Top up balance so retry will succeed
        engine.top_up_balance(customer.id, 10000)

        # Execute retry
        retry = engine.execute_retry(retry_id)
        assert retry.status == RetryStatus.SUCCEEDED
        assert retry.succeeded_payment_id is not None
        assert retry.current_attempt == 1
        assert len(retry.attempts) == 1
        assert retry.attempts[0].status == PaymentStatus.SUCCEEDED
        assert retry.completed_at is not None

    def test_retry_exhausted_after_max_attempts(self, engine, customer):
        """Retry exhausts when all attempts fail."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            max_attempts=3,
            initial_delay_seconds=0,  # No delay for testing
        )
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        # Execute all 3 attempts (all fail — no balance)
        retry = engine.execute_retry(retry_id)
        assert retry.status == RetryStatus.ACTIVE
        assert retry.current_attempt == 1

        retry = engine.execute_retry(retry_id)
        assert retry.status == RetryStatus.ACTIVE
        assert retry.current_attempt == 2

        retry = engine.execute_retry(retry_id)
        assert retry.status == RetryStatus.EXHAUSTED
        assert retry.current_attempt == 3
        assert retry.final_status == PaymentStatus.FAILED
        assert retry.completed_at is not None
        assert len(retry.attempts) == 3
        # All attempts failed
        for attempt in retry.attempts:
            assert attempt.status == PaymentStatus.FAILED

    def test_cancel_retry(self, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        retry = engine.cancel_retry(retry_id)
        assert retry.status == RetryStatus.CANCELLED
        assert retry.next_attempt_at is None
        assert retry.completed_at is not None

    def test_cancel_non_active_raises(self, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]
        engine.cancel_retry(retry_id)

        with pytest.raises(ValueError, match="Cannot cancel"):
            engine.cancel_retry(retry_id)

    def test_pause_and_resume(self, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        paused = engine.pause_retry(retry_id)
        assert paused.status == RetryStatus.PAUSED

        resumed = engine.resume_retry(retry_id)
        assert resumed.status == RetryStatus.ACTIVE

    def test_execute_on_exhausted_raises(self, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            max_attempts=1,
            initial_delay_seconds=0,
        )
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        engine.execute_retry(retry_id)  # Uses the 1 attempt

        with pytest.raises(ValueError, match="not active"):
            engine.execute_retry(retry_id)


# ── Auto Top-Up ───────────────────────────────────────────────────────

class TestAutoTopUp:
    def test_auto_top_up_before_retry(self, engine, customer):
        """Auto top-up adds balance before retry execution."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            auto_top_up=True,
            auto_top_up_amount=10000,  # $100
            max_attempts=3,
        )
        # Charge fails (0 balance)
        payment = engine.charge(customer_id=customer.id, amount=5000)
        retry_id = payment.metadata["retry_id"]

        # Verify auto top-up was applied
        retry = engine.get_retry(retry_id)
        assert retry.metadata.get("auto_top_up_applied") == 10000

        # Now balance should have the top-up
        cust = engine.storage.get_customer(customer.id)
        assert cust.balance == 10000

        # Execute retry — should succeed
        retry = engine.execute_retry(retry_id)
        assert retry.status == RetryStatus.SUCCEEDED

    def test_auto_top_up_on_each_failed_attempt(self, engine, customer):
        """Auto top-up applies before every retry attempt, not just the first."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            auto_top_up=True,
            auto_top_up_amount=1000,  # $10 — less than charge amount
            max_attempts=3,
            initial_delay_seconds=0,
        )
        payment = engine.charge(customer_id=customer.id, amount=5000)  # Need $50
        retry_id = payment.metadata["retry_id"]

        # Attempt 1 — fails (balance 1000 < 5000)
        retry = engine.execute_retry(retry_id)
        assert retry.current_attempt == 1
        # Auto top-up applied again before attempt 2
        assert retry.metadata.get("auto_top_up_count", 0) >= 1


# ── Process Pending Retries ──────────────────────────────────────────

class TestProcessPending:
    def test_process_due_retries(self, engine, customer):
        """process_pending_retries executes retries whose time has come."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=0,  # Due immediately
            max_attempts=2,
        )
        # Create multiple failed payments
        p1 = engine.charge(customer_id=customer.id, amount=1000)
        p2 = engine.charge(customer_id=customer.id, amount=2000)
        p3 = engine.charge(customer_id=customer.id, amount=3000)

        # Top up so retries succeed
        engine.top_up_balance(customer.id, 100000)

        processed = engine.process_pending_retries()
        assert len(processed) == 3

        for retry in processed:
            assert retry.status == RetryStatus.SUCCEEDED

    def test_process_skips_not_due(self, engine, customer):
        """Retries with future next_attempt_at are skipped."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=3600,  # 1 hour in future
        )
        payment = engine.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        processed = engine.process_pending_retries()
        assert len(processed) == 0

    def test_process_filtered_by_customer(self, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=0,
        )
        other = engine.create_customer(name="Other")

        engine.charge(customer_id=customer.id, amount=1000)
        engine.charge(customer_id=other.id, amount=1000)

        processed = engine.process_pending_retries(customer_id=customer.id)
        assert len(processed) == 1
        assert processed[0].customer_id == customer.id


# ── Retry Stats ───────────────────────────────────────────────────────

class TestRetryStats:
    def test_empty_stats(self, engine):
        stats = engine.get_retry_stats()
        assert stats["total_retries"] == 0
        assert stats["recovery_rate"] == 0.0
        assert stats["recovered_amount"] == 0.0

    def test_stats_with_mixed_outcomes(self, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            max_attempts=3,
            initial_delay_seconds=0,
        )
        funded = engine.create_customer(name="Funded")

        # Create 3 failed charges
        for i in range(3):
            engine.charge(customer_id=customer.id, amount=1000)

        # Top up and process retries — some will succeed
        engine.top_up_balance(customer.id, 1500)  # Only enough for ~1
        engine.process_pending_retries()

        stats = engine.get_retry_stats()
        assert stats["total_retries"] == 3
        assert "by_status" in stats
        assert "by_trigger" in stats
        assert "insufficient_balance" in stats["by_trigger"]

    def test_stats_recovered_amount(self, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=0,
        )
        payment = engine.charge(customer_id=customer.id, amount=5000)
        engine.top_up_balance(customer.id, 10000)

        engine.process_pending_retries()

        stats = engine.get_retry_stats()
        assert stats["succeeded"] == 1
        assert stats["recovered_amount"] == 5000.0


# ── Storage Persistence ───────────────────────────────────────────────

class TestPersistence:
    def test_retry_config_persists(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)
        config = engine1.configure_retry(
            customer_id=None,
            max_attempts=7,
            name="persistent",
        )

        # New storage instance loading from same path
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        configs = engine2.list_retry_configs()
        assert len(configs) == 1
        assert configs[0].max_attempts == 7

    def test_payment_retry_persists(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)
        customer = engine1.create_customer(name="Test")
        engine1.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine1.charge(customer_id=customer.id, amount=1000)
        retry_id = payment.metadata["retry_id"]

        # Reload
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        retry = engine2.get_retry(retry_id)
        assert retry is not None
        assert retry.original_payment_id == payment.id


# ── MCP Server Integration ────────────────────────────────────────────

class TestMCPServer:
    @pytest.fixture
    def server(self, engine):
        return MCPServer(engine=engine)

    def test_list_tools_includes_retry_tools(self, server):
        tools = server.list_tools()
        names = [t["name"] for t in tools]
        assert "configure_retry" in names
        assert "get_retry" in names
        assert "list_retries" in names
        assert "execute_retry" in names
        assert "process_pending_retries" in names
        assert "cancel_retry" in names
        assert "get_retry_stats" in names

    def test_tool_count(self, server):
        """Should now have 69 tools (was 60, +9 webhook tools)."""
        tools = server.list_tools()
        assert len(tools) == 71

    def test_configure_retry_via_server(self, server, customer):
        result = server.call_tool("configure_retry", {
            "customer_id": customer.id,
            "max_attempts": 5,
            "strategy": "exponential",
            "triggers": ["insufficient_balance", "provider_timeout"],
        })
        assert "error" not in result
        data = result["result"]
        assert data["max_attempts"] == 5
        assert data["strategy"] == "exponential"
        assert "insufficient_balance" in data["triggers"]

    def test_get_retry_via_server(self, server, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine.charge(customer_id=customer.id, amount=1000)

        result = server.call_tool("get_retry", {"retry_id": payment.metadata["retry_id"]})
        assert "error" not in result
        data = result["result"]
        assert data["status"] == "active"
        assert data["amount"] == 1000

    def test_list_retries_via_server(self, server, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        engine.charge(customer_id=customer.id, amount=1000)
        engine.charge(customer_id=customer.id, amount=2000)

        result = server.call_tool("list_retries", {"customer_id": customer.id})
        assert "error" not in result
        data = result["result"]
        assert data["count"] == 2

    def test_execute_retry_via_server(self, server, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=0,
        )
        payment = engine.charge(customer_id=customer.id, amount=1000)
        engine.top_up_balance(customer.id, 5000)

        result = server.call_tool("execute_retry", {"retry_id": payment.metadata["retry_id"]})
        assert "error" not in result
        data = result["result"]
        assert data["status"] == "succeeded"

    def test_cancel_retry_via_server(self, server, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        payment = engine.charge(customer_id=customer.id, amount=1000)

        result = server.call_tool("cancel_retry", {"retry_id": payment.metadata["retry_id"]})
        assert "error" not in result
        data = result["result"]
        assert data["status"] == "cancelled"

    def test_get_retry_stats_via_server(self, server, engine, customer):
        engine.configure_retry(customer_id=None, triggers=[RetryTrigger.INSUFFICIENT_BALANCE])
        engine.charge(customer_id=customer.id, amount=1000)

        result = server.call_tool("get_retry_stats", {})
        assert "error" not in result
        data = result["result"]
        assert data["total_retries"] == 1
        assert data["active"] == 1

    def test_process_pending_via_server(self, server, engine, customer):
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            initial_delay_seconds=0,
        )
        engine.charge(customer_id=customer.id, amount=1000)
        engine.top_up_balance(customer.id, 5000)

        result = server.call_tool("process_pending_retries", {"customer_id": customer.id})
        assert "error" not in result
        data = result["result"]
        assert data["processed_count"] == 1


# ── End-to-End Flow ────────────────────────────────────────────────────

class TestEndToEnd:
    def test_full_recovery_flow(self, engine, customer):
        """Complete flow: charge fails → auto-retry created → top-up → retry succeeds."""
        # 1. Configure retry globally
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.INSUFFICIENT_BALANCE],
            max_attempts=5,
            strategy=RetryStrategy.EXPONENTIAL,
            initial_delay_seconds=0,
        )

        # 2. Attempt charge — fails (no balance)
        payment = engine.charge(
            customer_id=customer.id,
            amount=2500,
            tool_name="premium_api",
            description="Premium API call",
        )
        assert payment.status == PaymentStatus.FAILED
        assert "retry_id" in payment.metadata

        # 3. Verify retry was auto-created
        retry = engine.get_retry(payment.metadata["retry_id"])
        assert retry is not None
        assert retry.status == RetryStatus.ACTIVE
        assert retry.tool_name == "premium_api"

        # 4. Top up balance
        engine.top_up_balance(customer.id, 10000)

        # 5. Process pending retries — should succeed
        processed = engine.process_pending_retries()
        assert len(processed) == 1
        assert processed[0].status == RetryStatus.SUCCEEDED

        # 6. Verify stats reflect recovery
        stats = engine.get_retry_stats()
        assert stats["succeeded"] == 1
        assert stats["recovery_rate"] == 100.0
        assert stats["recovered_amount"] == 2500.0

    def test_retry_with_different_providers(self, engine, customer):
        """Retry works with different payment providers."""
        engine.configure_retry(
            customer_id=None,
            triggers=[RetryTrigger.ALL_FAILURES],
            max_attempts=2,
            initial_delay_seconds=0,
        )
        # X402 payment without wallet — will fail
        payment = engine.charge(
            customer_id=customer.id,
            amount=1000,
            provider=PaymentProvider.X402,
        )
        assert payment.status == PaymentStatus.FAILED
        assert "retry_id" in payment.metadata

        retry = engine.get_retry(payment.metadata["retry_id"])
        assert retry.provider == PaymentProvider.X402
