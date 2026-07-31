"""Tests for v1.0.0 — Idempotency Keys (production-safe charge deduplication)."""
import pytest
import tempfile
import shutil
from pathlib import Path
from mcp_payments.engine import PaymentEngine
from mcp_payments.models import (
    IdempotencyRecord,
    PaymentStatus,
)
from mcp_payments.storage import Storage


@pytest.fixture
def engine():
    tmpdir = tempfile.mkdtemp(prefix="test-v1-idem-")
    yield PaymentEngine(storage=Storage(data_dir=tmpdir))
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def customer(engine):
    c = engine.create_customer(name="Test Agent", wallet_address="0xABC")
    engine.top_up_balance(c.id, 1_000_000)  # $10,000.00
    return c


# ── IdempotencyRecord Model ────────────────────────────────────────────

class TestIdempotencyRecord:
    def test_default_creation(self):
        record = IdempotencyRecord(key="abc123", customer_id="cus_test")
        assert record.key == "abc123"
        assert record.customer_id == "cus_test"
        assert record.operation == "charge"
        assert record.in_progress is False
        assert record.completed is False
        assert record.payment_id is None
        assert record.result is None
        assert record.error is None

    def test_with_all_fields(self):
        record = IdempotencyRecord(
            key="order-42",
            customer_id="cus_abc",
            operation="charge",
            in_progress=True,
            completed=False,
            payment_id="pay_xyz",
            result={"status": "succeeded", "amount": 500},
        )
        assert record.in_progress is True
        assert record.payment_id == "pay_xyz"
        assert record.result["status"] == "succeeded"

    def test_serialization(self):
        record = IdempotencyRecord(key="k1", customer_id="cus_1")
        data = record.model_dump(mode="json")
        assert data["key"] == "k1"
        assert data["customer_id"] == "cus_1"
        assert "created_at" in data
        assert "expires_at" in data


# ── Storage Layer ──────────────────────────────────────────────────────

class TestIdempotencyStorage:
    def test_create_and_get(self, engine):
        storage = engine.storage
        record = storage.create_idempotency_record(IdempotencyRecord(
            key="test-key-1",
            customer_id="cus_a",
            in_progress=True,
        ))
        assert record.key == "test-key-1"

        fetched = storage.get_idempotency_record("cus_a", "test-key-1")
        assert fetched is not None
        assert fetched.in_progress is True

    def test_get_nonexistent(self, engine):
        storage = engine.storage
        result = storage.get_idempotency_record("cus_nonexist", "no-key")
        assert result is None

    def test_update_record(self, engine):
        storage = engine.storage
        storage.create_idempotency_record(IdempotencyRecord(
            key="test-key-2",
            customer_id="cus_b",
            in_progress=True,
        ))

        updated = storage.update_idempotency_record(
            customer_id="cus_b",
            key="test-key-2",
            in_progress=False,
            completed=True,
            payment_id="pay_123",
        )
        assert updated is not None
        assert updated.in_progress is False
        assert updated.completed is True
        assert updated.payment_id == "pay_123"

    def test_update_nonexistent(self, engine):
        storage = engine.storage
        result = storage.update_idempotency_record(
            customer_id="cus_none",
            key="no-key",
            completed=True,
        )
        assert result is None

    def test_list_records(self, engine):
        storage = engine.storage
        storage.create_idempotency_record(IdempotencyRecord(
            key="list-1", customer_id="cus_list",
        ))
        storage.create_idempotency_record(IdempotencyRecord(
            key="list-2", customer_id="cus_list",
        ))
        storage.create_idempotency_record(IdempotencyRecord(
            key="list-3", customer_id="cus_other",
        ))

        all_records = storage.list_idempotency_records()
        assert len(all_records) >= 3

        customer_records = storage.list_idempotency_records(customer_id="cus_list")
        assert len(customer_records) == 2

    def test_delete_record(self, engine):
        storage = engine.storage
        storage.create_idempotency_record(IdempotencyRecord(
            key="del-1", customer_id="cus_del",
        ))

        assert storage.delete_idempotency_record("cus_del", "del-1") is True
        assert storage.get_idempotency_record("cus_del", "del-1") is None
        assert storage.delete_idempotency_record("cus_del", "del-1") is False

    def test_customer_scoped_keys(self, engine):
        """Same key for different customers should be separate records."""
        storage = engine.storage
        storage.create_idempotency_record(IdempotencyRecord(
            key="shared-key", customer_id="cus_x",
        ))
        storage.create_idempotency_record(IdempotencyRecord(
            key="shared-key", customer_id="cus_y",
        ))

        assert storage.get_idempotency_record("cus_x", "shared-key") is not None
        assert storage.get_idempotency_record("cus_y", "shared-key") is not None

    def test_expired_record_auto_removed(self, engine):
        from datetime import datetime, timedelta, timezone
        storage = engine.storage
        # Create a record that's already expired
        record = IdempotencyRecord(
            key="expired-key",
            customer_id="cus_exp",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        storage.create_idempotency_record(record)

        # Should return None and auto-delete
        result = storage.get_idempotency_record("cus_exp", "expired-key")
        assert result is None

    def test_cleanup_expired(self, engine):
        from datetime import datetime, timedelta, timezone
        storage = engine.storage
        # Create expired records
        for i in range(3):
            storage.create_idempotency_record(IdempotencyRecord(
                key=f"cleanup-{i}",
                customer_id="cus_clean",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ))
        # Create a valid record
        storage.create_idempotency_record(IdempotencyRecord(
            key="valid-key",
            customer_id="cus_clean",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=23),
        ))

        removed = storage.cleanup_expired_idempotency()
        assert removed >= 3
        assert storage.get_idempotency_record("cus_clean", "valid-key") is not None


# ── Engine: Charge with Idempotency ────────────────────────────────────

class TestChargeIdempotency:
    def test_idempotent_charge_returns_same_payment(self, engine, customer):
        """Calling charge twice with the same key returns the same payment."""
        key = "order-idempotent-001"

        # First charge
        p1 = engine.charge(
            customer_id=customer.id,
            amount=500,
            idempotency_key=key,
        )
        assert p1.status == PaymentStatus.SUCCEEDED

        # Second charge with same key — should return same payment
        p2 = engine.charge(
            customer_id=customer.id,
            amount=500,
            idempotency_key=key,
        )
        assert p2.id == p1.id
        assert p2.amount == p1.amount

    def test_different_keys_create_different_payments(self, engine, customer):
        p1 = engine.charge(customer_id=customer.id, amount=300, idempotency_key="key-a")
        p2 = engine.charge(customer_id=customer.id, amount=300, idempotency_key="key-b")
        assert p1.id != p2.id

    def test_charge_without_key_works_normally(self, engine, customer):
        p1 = engine.charge(customer_id=customer.id, amount=200)
        p2 = engine.charge(customer_id=customer.id, amount=200)
        assert p1.id != p2.id

    def test_idempotent_charge_different_amounts_same_key(self, engine, customer):
        """Same key with different amounts still returns the original payment."""
        key = "replay-test"

        p1 = engine.charge(customer_id=customer.id, amount=100, idempotency_key=key)
        p2 = engine.charge(customer_id=customer.id, amount=999, idempotency_key=key)

        # Returns original payment, ignores the new amount
        assert p2.id == p1.id
        assert p2.amount == 100

    def test_idempotency_scoped_per_customer(self, engine):
        """Same key for different customers creates separate payments."""
        c1 = engine.create_customer(name="C1", wallet_address="0x1")
        engine.top_up_balance(c1.id, 1_000_000)

        c2 = engine.create_customer(name="C2", wallet_address="0x2")
        engine.top_up_balance(c2.id, 1_000_000)

        key = "shared-order-key"
        p1 = engine.charge(customer_id=c1.id, amount=500, idempotency_key=key)
        p2 = engine.charge(customer_id=c2.id, amount=500, idempotency_key=key)

        assert p1.id != p2.id
        assert p1.customer_id == c1.id
        assert p2.customer_id == c2.id

    def test_in_progress_blocks_concurrent(self, engine, customer):
        """A key marked in_progress should block concurrent calls."""
        from mcp_payments.models import IdempotencyRecord
        from datetime import datetime, timedelta, timezone

        engine.storage.create_idempotency_record(IdempotencyRecord(
            key="concurrent-key",
            customer_id=customer.id,
            in_progress=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))

        with pytest.raises(ValueError, match="already being processed"):
            engine.charge(
                customer_id=customer.id,
                amount=500,
                idempotency_key="concurrent-key",
            )

    def test_balance_not_deducted_twice(self, engine, customer):
        """Critical: idempotent retry should NOT deduct balance twice."""
        key = "balance-test-key"

        balance_before = engine.storage.get_customer(customer.id).balance

        # Charge
        engine.charge(customer_id=customer.id, amount=5000, idempotency_key=key)

        balance_after_first = engine.storage.get_customer(customer.id).balance
        assert balance_after_first == balance_before - 5000

        # Retry with same key
        engine.charge(customer_id=customer.id, amount=5000, idempotency_key=key)

        balance_after_retry = engine.storage.get_customer(customer.id).balance
        # Balance should NOT change on retry
        assert balance_after_retry == balance_after_first
