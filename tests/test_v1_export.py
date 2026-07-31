"""Tests for v1.0.0 — Data Export and System Health."""
import json
import pytest
import tempfile
import shutil
from mcp_payments.engine import PaymentEngine
from mcp_payments.storage import Storage


@pytest.fixture
def engine():
    tmpdir = tempfile.mkdtemp(prefix="test-v1-export-")
    yield PaymentEngine(storage=Storage(data_dir=tmpdir))
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def populated_engine(engine):
    """Engine with some data for export testing."""
    # Create customer with balance
    c = engine.create_customer(name="Export Test", wallet_address="0xABC")
    engine.top_up_balance(c.id, 500_000)

    # Create some payments
    engine.charge(customer_id=c.id, amount=500, description="Payment 1")
    engine.charge(customer_id=c.id, amount=1000, description="Payment 2")

    # Create pricing
    engine.set_price(tool_name="premium-tool", amount=50)

    # Create escrow
    c2 = engine.create_customer(name="Payee", wallet_address="0xDEF")
    engine.top_up_balance(c2.id, 1_000_000)
    engine.create_escrow(
        payer_customer_id=c.id,
        payee_customer_id=c2.id,
        amount=2000,
        task_description="Test task",
    )

    # Create spend policy
    engine.set_spend_policy(
        customer_id=c.id,
        name="Test Policy",
        daily_limit=10000,
    )

    return engine


# ── Data Export ────────────────────────────────────────────────────────

class TestDataExport:
    def test_export_payments_json(self, populated_engine):
        result = populated_engine.export_data(resource="payments", format="json")
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) >= 2
        assert "id" in data[0]
        assert "customer_id" in data[0]
        assert "amount" in data[0]
        assert "status" in data[0]

    def test_export_payments_csv(self, populated_engine):
        result = populated_engine.export_data(resource="payments", format="csv")
        assert "id" in result  # header row
        assert "customer_id" in result
        lines = result.strip().split("\n")
        assert len(lines) >= 2  # header + at least one row

    def test_export_customers(self, populated_engine):
        result = populated_engine.export_data(resource="customers", format="json")
        data = json.loads(result)
        assert len(data) >= 2
        assert any(c["name"] == "Export Test" for c in data)

    def test_export_escrows(self, populated_engine):
        result = populated_engine.export_data(resource="escrows", format="json")
        data = json.loads(result)
        assert len(data) >= 1

    def test_export_spend_policies(self, populated_engine):
        result = populated_engine.export_data(resource="spend_policies", format="json")
        data = json.loads(result)
        assert len(data) >= 1

    def test_export_with_customer_filter(self, populated_engine):
        customers = populated_engine.storage.list_customers()
        cid = customers[0].id

        result = populated_engine.export_data(
            resource="payments", customer_id=cid, format="json",
        )
        data = json.loads(result)
        for p in data:
            assert p["customer_id"] == cid

    def test_export_empty_resource(self, engine):
        """Exporting with no data returns empty list/array."""
        result = engine.export_data(resource="payments", format="json")
        assert json.loads(result) == []

    def test_export_empty_csv(self, engine):
        result = engine.export_data(resource="payments", format="csv")
        assert result == ""

    def test_export_invalid_resource(self, engine):
        with pytest.raises(ValueError, match="Unknown resource"):
            engine.export_data(resource="nonexistent")

    def test_export_invalid_format(self, populated_engine):
        with pytest.raises(ValueError, match="Unknown format"):
            populated_engine.export_data(resource="payments", format="xml")

    def test_export_limit(self, populated_engine):
        result = populated_engine.export_data(resource="payments", limit=1, format="json")
        data = json.loads(result)
        assert len(data) <= 1

    def test_export_status_filter(self, populated_engine):
        result = populated_engine.export_data(
            resource="payments", status="succeeded", format="json",
        )
        data = json.loads(result)
        for p in data:
            assert p["status"] == "succeeded"


# ── System Health ──────────────────────────────────────────────────────

class TestSystemHealth:
    def test_health_basic_structure(self, engine):
        health = engine.system_health()
        assert "version" in health
        assert "timestamp" in health
        assert "customers" in health
        assert "payments" in health
        assert "escrows" in health
        assert "disputes" in health
        assert "marketplace" in health
        assert "idempotency" in health
        assert "status" in health

    def test_health_version(self, engine):
        health = engine.system_health()
        assert health["version"] == "1.0.0"

    def test_health_status_healthy(self, engine):
        health = engine.system_health()
        assert health["status"] == "healthy"

    def test_health_with_data(self, populated_engine):
        health = populated_engine.system_health()

        assert health["customers"]["total"] >= 2
        assert health["payments"]["total"] >= 2
        assert health["payments"]["succeeded"] >= 2
        assert health["payments"]["success_rate"] > 0
        assert health["payments"]["total_volume_cents"] > 0
        assert health["escrows"]["total"] >= 1

    def test_health_customer_stats(self, populated_engine):
        health = populated_engine.system_health()
        assert health["customers"]["total_balance_cents"] > 0

    def test_health_payment_calculations(self, populated_engine):
        health = populated_engine.system_health()

        # Success rate should be valid percentage
        assert 0 <= health["payments"]["success_rate"] <= 100

        # Succeeded + failed should equal total (for these simple tests)
        p = health["payments"]
        assert p["succeeded"] + p["failed"] <= p["total"]

    def test_health_empty_engine(self, engine):
        """Health check on an engine with no data."""
        health = engine.system_health()
        assert health["customers"]["total"] == 0
        assert health["payments"]["total"] == 0
        assert health["payments"]["success_rate"] == 0.0
        assert health["escrows"]["total"] == 0
        assert health["disputes"]["total"] == 0
        assert health["marketplace"]["total_services"] == 0
