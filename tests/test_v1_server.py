"""Tests for v1.0.0 — New MCP tools via server layer."""
import json
import pytest
import tempfile
import shutil
from mcp_payments.server import MCPServer, TOOL_DEFINITIONS
from mcp_payments.engine import PaymentEngine
from mcp_payments.storage import Storage


@pytest.fixture
def server():
    tmpdir = tempfile.mkdtemp(prefix="test-v1-server-")
    engine = PaymentEngine(storage=Storage(data_dir=tmpdir))
    yield MCPServer(engine=engine)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def populated_server():
    tmpdir = tempfile.mkdtemp(prefix="test-v1-server-pop-")
    engine = PaymentEngine(storage=Storage(data_dir=tmpdir))
    server = MCPServer(engine=engine)

    # Create customer
    result = server.call_tool("create_customer", {"name": "Server Test"})
    cus_id = result["result"]["customer_id"]

    # Top up
    server.call_tool("top_up_balance", {"customer_id": cus_id, "amount": 1_000_000})

    # Charge
    server.call_tool("charge", {
        "customer_id": cus_id, "amount": 500, "description": "Test charge",
    })

    yield server, cus_id
    shutil.rmtree(tmpdir, ignore_errors=True)


# ── Tool Definitions ───────────────────────────────────────────────────

class TestV1ToolDefinitions:
    def test_tool_count_is_71(self):
        assert len(TOOL_DEFINITIONS) == 71

    def test_export_data_tool_exists(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "export_data" in names

    def test_system_health_tool_exists(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "system_health" in names

    def test_export_data_has_resource_param(self):
        export_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "export_data")
        props = export_tool["inputSchema"]["properties"]
        assert "resource" in props
        assert "format" in props
        assert "customer_id" in props
        assert "status" in props

    def test_charge_has_idempotency_key(self):
        charge_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "charge")
        props = charge_tool["inputSchema"]["properties"]
        assert "idempotency_key" in props


# ── Server: Charge with Idempotency ────────────────────────────────────

class TestServerIdempotency:
    def test_charge_with_idempotency_key(self, populated_server):
        server, cus_id = populated_server

        # First charge
        r1 = server.call_tool("charge", {
            "customer_id": cus_id,
            "amount": 300,
            "idempotency_key": "order-123",
        })
        assert "result" in r1
        p1_id = r1["result"]["payment_id"]

        # Retry same key
        r2 = server.call_tool("charge", {
            "customer_id": cus_id,
            "amount": 300,
            "idempotency_key": "order-123",
        })
        assert r2["result"]["payment_id"] == p1_id

    def test_charge_without_key_creates_new(self, populated_server):
        server, cus_id = populated_server

        r1 = server.call_tool("charge", {"customer_id": cus_id, "amount": 100})
        r2 = server.call_tool("charge", {"customer_id": cus_id, "amount": 100})

        assert r1["result"]["payment_id"] != r2["result"]["payment_id"]


# ── Server: Export Data ────────────────────────────────────────────────

class TestServerExport:
    def test_export_payments_json(self, populated_server):
        server, cus_id = populated_server
        result = server.call_tool("export_data", {"resource": "payments"})
        assert "result" in result
        data = json.loads(result["result"]["data"])
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_export_payments_csv(self, populated_server):
        server, cus_id = populated_server
        result = server.call_tool("export_data", {
            "resource": "payments", "format": "csv",
        })
        assert "result" in result
        assert "id" in result["result"]["data"]

    def test_export_customers(self, populated_server):
        server, cus_id = populated_server
        result = server.call_tool("export_data", {"resource": "customers"})
        data = json.loads(result["result"]["data"])
        assert len(data) >= 1

    def test_export_invalid_resource(self, server):
        result = server.call_tool("export_data", {"resource": "nonexistent"})
        assert "error" in result


# ── Server: System Health ──────────────────────────────────────────────

class TestServerHealth:
    def test_system_health(self, populated_server):
        server, cus_id = populated_server
        result = server.call_tool("system_health", {})
        assert "result" in result
        health = result["result"]
        assert health["version"] == "1.0.0"
        assert health["status"] == "healthy"
        assert health["customers"]["total"] >= 1
        assert health["payments"]["total"] >= 1

    def test_system_health_empty(self, server):
        result = server.call_tool("system_health", {})
        health = result["result"]
        assert health["customers"]["total"] == 0
        assert health["payments"]["total"] == 0
