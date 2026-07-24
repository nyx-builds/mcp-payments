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
        assert len(TOOL_DEFINITIONS) == 44

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
            # v0.3.0
            "verify_x402_payment", "create_x402_middleware_config",
            # v0.4.0
            "record_usage", "get_usage_summary", "settle_usage", "list_usage_events",
            # v0.5.0
            "register_service", "publish_service", "search_services",
            "list_services", "get_service", "purchase_service",
            "create_plan", "subscribe_to_plan",
            "review_service", "list_service_reviews",
            # v0.6.0
            "set_spend_policy", "check_authorization", "get_spend_report",
            "list_spend_policies", "delete_spend_policy",
        }
        assert names == expected


class TestToolList:
    def test_list_tools(self, server):
        tools = server.list_tools()
        assert len(tools) == 44

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

    # ── v0.3.0: x402 middleware tools ─────────────────────────────────

    def test_verify_x402_payment_valid(self, tmp_path):
        from mcp_payments.middleware import make_payment_header
        storage = Storage(data_dir=str(tmp_path / "payments"))
        engine = PaymentEngine(storage=storage, merchant_wallet="0xMerchant")
        srv = MCPServer(engine=engine)
        header = make_payment_header(0.01, "0xMerchant")
        result = srv.call_tool("verify_x402_payment", {
            "payment_header": header,
            "amount": 0.01,
            "merchant_wallet": "0xMerchant",
        })
        assert result["result"]["valid"] is True
        assert result["result"]["transaction_id"]

    def test_verify_x402_payment_invalid_amount(self, tmp_path):
        from mcp_payments.middleware import make_payment_header
        storage = Storage(data_dir=str(tmp_path / "payments"))
        engine = PaymentEngine(storage=storage, merchant_wallet="0xMerchant")
        srv = MCPServer(engine=engine)
        header = make_payment_header(0.99, "0xMerchant")  # Wrong amount
        result = srv.call_tool("verify_x402_payment", {
            "payment_header": header,
            "amount": 0.01,
            "merchant_wallet": "0xMerchant",
        })
        assert result["result"]["valid"] is False
        assert "Amount mismatch" in result["result"]["reason"]

    def test_verify_x402_payment_no_header(self, tmp_path):
        storage = Storage(data_dir=str(tmp_path / "payments"))
        engine = PaymentEngine(storage=storage, merchant_wallet="0xMerchant")
        srv = MCPServer(engine=engine)
        result = srv.call_tool("verify_x402_payment", {
            "payment_header": "",
            "amount": 0.01,
        })
        assert result["result"]["valid"] is False

    def test_create_x402_middleware_config(self, server):
        result = server.call_tool("create_x402_middleware_config", {
            "merchant_wallet": "0xMerchant",
            "rules": [
                {"method": "GET", "path": "/api/data", "amount": 0.01},
                {"method": "POST", "path": "/api/analyze", "amount": 0.05},
            ],
        })
        assert "result" in result
        assert result["result"]["merchant_wallet"] == "0xMerchant"
        assert len(result["result"]["rules"]) == 2
        assert result["result"]["rules"][0]["amount_atomic"] == "10000"
        assert "X402Middleware" in result["result"]["python_snippet"]


# ── v0.4.0: Usage Metering MCP tool tests ────────────────────────────────────

class TestUsageMeteringTools:
    """Tests for v0.4.0 metering tools exposed via MCP server."""

    def test_record_usage(self, server):
        cust = server.call_tool("create_customer", {"name": "Metered"})
        cid = cust["result"]["customer_id"]
        result = server.call_tool("record_usage", {
            "customer_id": cid,
            "tool_name": "search_web",
            "unit": "calls",
            "quantity": 1,
        })
        assert "result" in result
        assert result["result"]["event_id"].startswith("usage_")
        assert result["result"]["tool_name"] == "search_web"
        assert result["result"]["unit"] == "calls"
        assert result["result"]["quantity"] == 1
        assert result["result"]["settled"] is False

    def test_record_usage_token_auto_quantity(self, server):
        """Token-based usage auto-computes quantity from input+output."""
        cust = server.call_tool("create_customer", {"name": "Token Agent"})
        cid = cust["result"]["customer_id"]
        result = server.call_tool("record_usage", {
            "customer_id": cid,
            "tool_name": "llm_complete",
            "unit": "tokens",
            "input_tokens": 500,
            "output_tokens": 300,
        })
        assert result["result"]["quantity"] == 800

    def test_record_usage_defaults(self, server):
        """Minimal args — defaults to 1 call."""
        cust = server.call_tool("create_customer", {"name": "Default"})
        cid = cust["result"]["customer_id"]
        result = server.call_tool("record_usage", {
            "customer_id": cid,
            "tool_name": "ping",
        })
        assert result["result"]["unit"] == "calls"
        assert result["result"]["quantity"] == 1

    def test_record_usage_unknown_customer(self, server):
        result = server.call_tool("record_usage", {
            "customer_id": "cus_nonexistent",
            "tool_name": "search",
        })
        assert "error" in result

    def test_get_usage_summary_empty(self, server):
        cust = server.call_tool("create_customer", {"name": "Empty Usage"})
        cid = cust["result"]["customer_id"]
        result = server.call_tool("get_usage_summary", {"customer_id": cid})
        assert result["result"]["total_events"] == 0
        assert result["result"]["estimated_cost"] == 0
        assert result["result"]["unsettled_events"] == 0

    def test_get_usage_summary_with_events(self, server):
        cust = server.call_tool("create_customer", {"name": "Active"})
        cid = cust["result"]["customer_id"]
        for _ in range(10):
            server.call_tool("record_usage", {"customer_id": cid, "tool_name": "search"})
        result = server.call_tool("get_usage_summary", {"customer_id": cid})
        assert result["result"]["total_events"] == 10
        assert result["result"]["total_by_unit"]["calls"] == 10
        assert result["result"]["unsettled_events"] == 10

    def test_get_usage_summary_with_pricing(self, server):
        cust = server.call_tool("create_customer", {"name": "Priced"})
        cid = cust["result"]["customer_id"]
        server.call_tool("set_tool_price", {"tool_name": "search", "amount": 1, "pricing_model": "per_use"})
        for _ in range(5):
            server.call_tool("record_usage", {"customer_id": cid, "tool_name": "search"})
        result = server.call_tool("get_usage_summary", {"customer_id": cid})
        assert result["result"]["estimated_cost"] == 5.0

    def test_settle_usage(self, server):
        cust = server.call_tool("create_customer", {"name": "Settle Test"})
        cid = cust["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        server.call_tool("set_tool_price", {"tool_name": "search", "amount": 2, "pricing_model": "per_use"})
        for _ in range(10):
            server.call_tool("record_usage", {"customer_id": cid, "tool_name": "search"})

        result = server.call_tool("settle_usage", {"customer_id": cid})
        assert result["result"]["events_settled"] == 10
        assert result["result"]["total_charged"] == 20.0
        assert len(result["result"]["payment_ids"]) == 1

    def test_settle_usage_idempotent(self, server):
        cust = server.call_tool("create_customer", {"name": "Idempotent"})
        cid = cust["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        server.call_tool("set_tool_price", {"tool_name": "search", "amount": 1, "pricing_model": "per_use"})
        server.call_tool("record_usage", {"customer_id": cid, "tool_name": "search"})

        first = server.call_tool("settle_usage", {"customer_id": cid})
        second = server.call_tool("settle_usage", {"customer_id": cid})

        assert first["result"]["events_settled"] == 1
        assert second["result"]["events_settled"] == 0
        assert second["result"]["total_charged"] == 0

    def test_list_usage_events(self, server):
        cust = server.call_tool("create_customer", {"name": "List Events"})
        cid = cust["result"]["customer_id"]
        server.call_tool("record_usage", {"customer_id": cid, "tool_name": "a"})
        server.call_tool("record_usage", {"customer_id": cid, "tool_name": "b"})

        result = server.call_tool("list_usage_events", {"customer_id": cid})
        assert result["result"]["count"] == 2

    def test_list_usage_events_filter_settled(self, server):
        cust = server.call_tool("create_customer", {"name": "Filter"})
        cid = cust["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 10000})
        server.call_tool("set_tool_price", {"tool_name": "t", "amount": 1, "pricing_model": "per_use"})
        server.call_tool("record_usage", {"customer_id": cid, "tool_name": "t"})
        server.call_tool("settle_usage", {"customer_id": cid})

        unsettled = server.call_tool("list_usage_events", {"customer_id": cid, "settled": False})
        settled = server.call_tool("list_usage_events", {"customer_id": cid, "settled": True})

        assert unsettled["result"]["count"] == 0
        assert settled["result"]["count"] == 1

    def test_full_metered_cycle_via_mcp(self, server):
        """End-to-end: create customer → set price → record → summary → settle."""
        # 1. Setup
        cust = server.call_tool("create_customer", {"name": "E2E Metered"})
        cid = cust["result"]["customer_id"]
        server.call_tool("top_up_balance", {"customer_id": cid, "amount": 100000})
        server.call_tool("set_tool_price", {"tool_name": "api", "amount": 5, "pricing_model": "per_use"})

        # 2. Record usage
        for _ in range(20):
            server.call_tool("record_usage", {"customer_id": cid, "tool_name": "api"})

        # 3. Summary
        summary = server.call_tool("get_usage_summary", {"customer_id": cid})
        assert summary["result"]["total_events"] == 20
        assert summary["result"]["estimated_cost"] == 100.0

        # 4. Settle
        settlement = server.call_tool("settle_usage", {"customer_id": cid})
        assert settlement["result"]["events_settled"] == 20
        assert settlement["result"]["total_charged"] == 100.0

        # 5. Post-settle summary
        summary_after = server.call_tool("get_usage_summary", {"customer_id": cid})
        assert summary_after["result"]["settled_events"] == 20
        assert summary_after["result"]["unsettled_events"] == 0
