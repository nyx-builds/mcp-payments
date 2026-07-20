"""Tests for MCP server tool definitions and handlers."""
import pytest
from mcp_payments.engine import PaymentEngine
from mcp_payments.models import Currency, PaymentStatus, PaymentProvider
from mcp_payments.server import MCPServer, TOOL_DEFINITIONS
from mcp_payments.storage import Storage


@pytest.fixture
def server(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    engine = PaymentEngine(storage=storage)
    return MCPServer(engine=engine)


class TestToolDefinitions:
    def test_tool_count(self):
        assert len(TOOL_DEFINITIONS) == 23

    def test_all_have_names(self):
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool
            assert isinstance(tool["name"], str)

    def test_all_have_descriptions(self):
        for tool in TOOL_DEFINITIONS:
            assert "description" in tool
            assert len(tool["description"]) > 10

    def test_all_have_input_schemas(self):
        for tool in TOOL_DEFINITIONS:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_unique_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names))

    def test_expected_tool_names(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "set_tool_price", "get_tool_price", "list_tool_prices",
            "create_customer", "get_customer", "top_up_balance",
            "create_payment_intent", "charge", "fulfill_intent",
            "get_payment", "verify_payment", "refund_payment",
            "get_receipt", "list_payments", "payment_summary",
            "create_x402_response",
            # v0.2.0
            "create_escrow", "release_escrow", "refund_escrow",
            "get_escrow", "list_escrows",
            "create_split", "get_split",
        }
        assert names == expected


class TestToolList:
    def test_list_tools(self, server):
        tools = server.list_tools()
        assert len(tools) == 23

    def test_list_tools_returns_definitions(self, server):
        tools = server.list_tools()
        assert tools[0]["name"] == "set_tool_price"


class TestToolHandlers:
    def test_unknown_tool(self, server):
        result = server.call_tool("nonexistent", {})
        assert "error" in result

    def test_set_tool_price(self, server):
        result = server.call_tool("set_tool_price", {
            "tool_name": "search",
            "amount": 50,
            "currency": "USD",
        })
        assert "result" in result
        assert result["result"]["tool_name"] == "search"

    def test_get_tool_price(self, server):
        server.call_tool("set_tool_price", {"tool_name": "search", "amount": 50})
        result = server.call_tool("get_tool_price", {"tool_name": "search"})
        assert result["result"]["amount"] == 50

    def test_get_nonexistent_price(self, server):
        result = server.call_tool("get_tool_price", {"tool_name": "nope"})
        assert "error" in result["result"]

    def test_list_tool_prices(self, server):
        server.call_tool("set_tool_price", {"tool_name": "a", "amount": 10})
        server.call_tool("set_tool_price", {"tool_name": "b", "amount": 20})
        result = server.call_tool("list_tool_prices", {})
        assert result["result"]["count"] == 2

    def test_create_customer(self, server):
        result = server.call_tool("create_customer", {"name": "Test Agent"})
        assert "customer_id" in result["result"]

    def test_get_customer(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        result = server.call_tool("get_customer", {"customer_id": cid})
        assert result["result"]["name"] == "Test"

    def test_get_nonexistent_customer(self, server):
        result = server.call_tool("get_customer", {"customer_id": "cus_xxx"})
        assert "error" in result["result"]

    def test_top_up_balance(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        result = server.call_tool("top_up_balance", {"customer_id": cid, "amount": 5000})
        assert result["result"]["new_balance"] == 5000

    def test_charge_flow(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        result = server.call_tool("charge", {
            "customer_id": cid,
            "amount": 500,
            "tool_name": "search",
        })
        assert result["result"]["status"] == "succeeded"

    def test_charge_insufficient_balance(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        result = server.call_tool("charge", {"customer_id": cid, "amount": 99999})
        assert result["result"]["status"] == "failed"

    def test_get_payment(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        charge = server.call_tool("charge", {"customer_id": cid, "amount": 500})
        pid = charge["result"]["payment_id"]
        result = server.call_tool("get_payment", {"payment_id": pid})
        assert result["result"]["payment_id"] == pid
        assert result["result"]["amount"] == 500

    def test_verify_payment(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        charge = server.call_tool("charge", {"customer_id": cid, "amount": 500})
        pid = charge["result"]["payment_id"]
        result = server.call_tool("verify_payment", {"payment_id": pid})
        assert result["result"]["valid"] is True

    def test_refund_flow(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        charge = server.call_tool("charge", {"customer_id": cid, "amount": 500})
        pid = charge["result"]["payment_id"]
        result = server.call_tool("refund_payment", {"payment_id": pid})
        assert result["result"]["status"] == "succeeded"

    def test_get_receipt(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        charge = server.call_tool("charge", {"customer_id": cid, "amount": 500})
        pid = charge["result"]["payment_id"]
        result = server.call_tool("get_receipt", {"payment_id": pid})
        assert "payment_id" in result["result"]

    def test_list_payments(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        server.call_tool("charge", {"customer_id": cid, "amount": 100})
        server.call_tool("charge", {"customer_id": cid, "amount": 200})
        result = server.call_tool("list_payments", {"customer_id": cid})
        assert result["result"]["count"] == 2

    def test_payment_summary(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        server.call_tool("charge", {"customer_id": cid, "amount": 500, "tool_name": "search"})
        result = server.call_tool("payment_summary", {})
        assert result["result"]["total_payments"] == 1
        assert result["result"]["succeeded"] == 1

    def test_create_intent_and_fulfill(self, server):
        create = server.call_tool("create_customer", {"name": "Test"})
        cid = create["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        intent = server.call_tool("create_payment_intent", {
            "customer_id": cid, "amount": 500,
        })
        iid = intent["result"]["intent_id"]
        result = server.call_tool("fulfill_intent", {"intent_id": iid})
        assert result["result"]["status"] == "succeeded"

    def test_create_x402_no_wallet(self, server):
        result = server.call_tool("create_x402_response", {"amount": 0.01})
        assert "error" in result

    def test_create_x402_with_wallet(self, tmp_path):
        storage = Storage(data_dir=str(tmp_path / "payments"))
        engine = PaymentEngine(storage=storage, merchant_wallet="0xMerchant")
        srv = MCPServer(engine=engine)
        result = srv.call_tool("create_x402_response", {
            "amount": 0.01,
            "resource_url": "https://api.example.com/premium",
        })
        assert "result" in result
        assert result["result"]["pay_to"] == "0xMerchant"

    def test_charge_with_error_handling(self, server):
        """Charge to nonexistent customer returns error in result."""
        result = server.call_tool("charge", {
            "customer_id": "cus_nonexistent",
            "amount": 100,
        })
        assert "error" in result
