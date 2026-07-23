"""MCP server — exposes payment tools via Model Context Protocol."""
from __future__ import annotations

import json
from typing import Any

from ..engine import PaymentEngine
from ..models import Currency, PaymentProvider, PaymentStatus, PricingModel, ServiceStatus, X402PaymentRequirements


# Tool schemas for MCP
TOOL_DEFINITIONS = [
    {
        "name": "set_tool_price",
        "description": "Set pricing for an MCP tool or service. Use this to monetize your agent's tools.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Name of the tool to price"},
                "amount": {"type": "number", "description": "Price amount (in USD cents for fiat, base units for crypto)"},
                "currency": {"type": "string", "enum": [c.value for c in Currency], "default": "USD"},
                "pricing_model": {"type": "string", "enum": [p.value for p in PricingModel], "default": "per_use"},
                "free_tier_limit": {"type": "integer", "description": "Number of free calls before pricing applies"},
                "description": {"type": "string", "default": ""},
            },
            "required": ["tool_name", "amount"],
        },
    },
    {
        "name": "get_tool_price",
        "description": "Check the current pricing for a tool.",
        "inputSchema": {
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
            "required": ["tool_name"],
        },
    },
    {
        "name": "list_tool_prices",
        "description": "List all tool pricing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_customer",
        "description": "Register a new customer (agent or user) for payments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "agent_id": {"type": "string", "description": "External agent ID (e.g. SAID Protocol ID)"},
                "wallet_address": {"type": "string", "description": "Crypto wallet for x402/on-chain payments"},
                "email": {"type": "string"},
            },
        },
    },
    {
        "name": "get_customer",
        "description": "Look up a customer by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "top_up_balance",
        "description": "Add prepaid balance to a customer account (internal ledger).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number", "description": "Amount to add in cents"},
            },
            "required": ["customer_id", "amount"],
        },
    },
    {
        "name": "create_payment_intent",
        "description": "Create a payment intent — declares intent to pay before execution. Compatible with x402 protocol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string", "enum": [c.value for c in Currency], "default": "USD"},
                "tool_name": {"type": "string"},
                "description": {"type": "string"},
                "provider": {"type": "string", "enum": [p.value for p in PaymentProvider], "default": "internal"},
            },
            "required": ["customer_id", "amount"],
        },
    },
    {
        "name": "charge",
        "description": "Charge a customer immediately. Creates and processes a payment in one step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "amount": {"type": "number", "description": "Amount in cents (fiat) or base units (crypto)"},
                "currency": {"type": "string", "enum": [c.value for c in Currency], "default": "USD"},
                "tool_name": {"type": "string"},
                "description": {"type": "string"},
                "provider": {"type": "string", "enum": [p.value for p in PaymentProvider], "default": "internal"},
            },
            "required": ["customer_id", "amount"],
        },
    },
    {
        "name": "fulfill_intent",
        "description": "Fulfill a payment intent — execute the charge.",
        "inputSchema": {
            "type": "object",
            "properties": {"intent_id": {"type": "string"}},
            "required": ["intent_id"],
        },
    },
    {
        "name": "get_payment",
        "description": "Get details of a specific payment.",
        "inputSchema": {
            "type": "object",
            "properties": {"payment_id": {"type": "string"}},
            "required": ["payment_id"],
        },
    },
    {
        "name": "verify_payment",
        "description": "Verify that a payment succeeded. Returns validity status and transaction details.",
        "inputSchema": {
            "type": "object",
            "properties": {"payment_id": {"type": "string"}},
            "required": ["payment_id"],
        },
    },
    {
        "name": "refund_payment",
        "description": "Refund a payment (full or partial). Credits back customer balance for internal payments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payment_id": {"type": "string"},
                "amount": {"type": "number", "description": "Refund amount. If omitted, refunds full amount."},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["payment_id"],
        },
    },
    {
        "name": "get_receipt",
        "description": "Generate a payment receipt with cryptographic signature.",
        "inputSchema": {
            "type": "object",
            "properties": {"payment_id": {"type": "string"}},
            "required": ["payment_id"],
        },
    },
    {
        "name": "list_payments",
        "description": "List payments with optional filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "status": {"type": "string", "enum": [s.value for s in PaymentStatus]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "payment_summary",
        "description": "Get payment analytics — total volume, success rate, revenue by tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Filter to specific customer (optional)"},
            },
        },
    },
    {
        "name": "create_x402_response",
        "description": "Generate x402 HTTP 402 payment requirements. Use when an agent requests a paid resource.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "USD amount (converted to USDC atomic units)"},
                "resource_url": {"type": "string", "description": "URL that requires payment"},
                "description": {"type": "string"},
                "network": {"type": "string", "default": "base-sepolia"},
            },
            "required": ["amount"],
        },
    },
    {
        "name": "create_escrow",
        "description": "Create an escrow that holds funds until a task between agents completes. Agent A funds escrow → Agent B does the task → Agent A releases. Solves agent-to-agent trust.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payer_customer_id": {"type": "string", "description": "Customer ID of the paying agent"},
                "payee_customer_id": {"type": "string", "description": "Customer ID of the agent doing the work"},
                "amount": {"type": "number", "description": "Amount to hold in escrow"},
                "currency": {"type": "string", "enum": [c.value for c in Currency], "default": "USD"},
                "task_description": {"type": "string", "description": "What the payee must do to earn release"},
                "task_id": {"type": "string", "description": "External task/job ID (optional)"},
                "tool_name": {"type": "string"},
                "expires_in_seconds": {"type": "integer", "description": "Auto-refund if not released by this time"},
            },
            "required": ["payer_customer_id", "payee_customer_id", "amount"],
        },
    },
    {
        "name": "release_escrow",
        "description": "Release escrow funds to the payee — call when the task is complete and you're satisfied.",
        "inputSchema": {
            "type": "object",
            "properties": {"escrow_id": {"type": "string"}},
            "required": ["escrow_id"],
        },
    },
    {
        "name": "refund_escrow",
        "description": "Refund escrow funds back to the payer — call if the task was not completed or was rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "escrow_id": {"type": "string"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["escrow_id"],
        },
    },
    {
        "name": "get_escrow",
        "description": "Check the status of an escrow.",
        "inputSchema": {
            "type": "object",
            "properties": {"escrow_id": {"type": "string"}},
            "required": ["escrow_id"],
        },
    },
    {
        "name": "list_escrows",
        "description": "List escrows, optionally filtered by payer, payee, or status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payer_id": {"type": "string"},
                "payee_id": {"type": "string"},
                "status": {"type": "string", "enum": ["held", "released", "refunded", "disputed"]},
            },
        },
    },
    {
        "name": "create_split",
        "description": "Split a payment across multiple recipients. e.g. Charge $10 → $7 to provider, $2 platform fee, $1 referrer. One charge in, many credits out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payer_customer_id": {"type": "string"},
                "shares": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "amount": {"type": "number", "description": "Fixed amount (use this OR percentage)"},
                            "percentage": {"type": "number", "description": "Percentage of total (0-100). Use this OR amount."},
                            "label": {"type": "string"},
                        },
                        "required": ["customer_id"],
                    },
                    "description": "List of recipients and their shares",
                },
                "currency": {"type": "string", "enum": [c.value for c in Currency], "default": "USD"},
                "source_payment_id": {"type": "string", "description": "Existing payment to split (optional)"},
                "tool_name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["payer_customer_id", "shares"],
        },
    },
    {
        "name": "get_split",
        "description": "Check the status of a split payment.",
        "inputSchema": {
            "type": "object",
            "properties": {"split_id": {"type": "string"}},
            "required": ["split_id"],
        },
    },
    # ── v0.3.0: x402 Billing Middleware ────────────────────────────────
    {
        "name": "verify_x402_payment",
        "description": "Verify an x402 payment header. Use this to validate that an agent has paid before serving a paid resource. The payment_header is the raw base64 X-PAYMENT header value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payment_header": {"type": "string", "description": "Base64-encoded X-PAYMENT header value"},
                "amount": {"type": "number", "description": "Required payment amount in USD"},
                "merchant_wallet": {"type": "string", "description": "Wallet address that should receive payment"},
                "network": {"type": "string", "default": "base-sepolia"},
            },
            "required": ["payment_header", "amount"],
        },
    },
    {
        "name": "create_x402_middleware_config",
        "description": "Generate x402 pricing middleware configuration. Returns the config needed to set up HTTP 402 payment enforcement on your ASGI app (FastAPI/Starlette). Supports multiple pricing rules for different endpoints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "merchant_wallet": {"type": "string", "description": "Wallet address to receive payments"},
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string", "default": "GET"},
                            "path": {"type": "string", "description": "URL path prefix to charge for"},
                            "amount": {"type": "number", "description": "USD amount per request"},
                            "description": {"type": "string"},
                        },
                        "required": ["path", "amount"],
                    },
                    "description": "Pricing rules for different endpoints",
                },
            },
            "required": ["merchant_wallet", "rules"],
        },
    },
    # ── v0.4.0: Usage Metering ────────────────────────────────────────
    {
        "name": "record_usage",
        "description": "Record a metered usage event from an agent. Call this every time an agent invokes a tool, consumes tokens, or uses a metered resource. Events accumulate and are settled (charged) later via settle_usage. Supports per-call, per-token, per-second, and custom billing units.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Customer ID of the agent using the tool"},
                "tool_name": {"type": "string", "description": "Name of the tool/resource consumed"},
                "unit": {"type": "string", "enum": ["calls", "tokens", "input_tokens", "output_tokens", "seconds", "requests", "bytes", "custom"], "default": "calls", "description": "Unit of measurement"},
                "quantity": {"type": "number", "description": "Amount consumed (e.g. 1 call, 1500 tokens, 30 seconds)", "default": 1},
                "session_id": {"type": "string", "description": "Agent session that generated this event"},
                "request_id": {"type": "string", "description": "Individual request/invocation ID"},
                "input_tokens": {"type": "integer", "description": "Input/prompt tokens (for token-based pricing; auto-added to quantity if quantity=1)"},
                "output_tokens": {"type": "integer", "description": "Output/completion tokens (for token-based pricing)"},
            },
            "required": ["customer_id", "tool_name"],
        },
    },
    {
        "name": "get_usage_summary",
        "description": "Get aggregated usage summary for a customer, optionally filtered by tool and time period. Returns total events, breakdown by unit, estimated cost based on current pricing, and settled/unsettled counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "tool_name": {"type": "string", "description": "Filter to a specific tool"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "settle_usage",
        "description": "Settle accumulated metered usage — charges the customer for all unsettled events. Groups by tool, computes cost from pricing, and creates charges. Call this at the end of a billing period. Idempotent: already-settled events are skipped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "tool_name": {"type": "string", "description": "Only settle usage for this tool (optional)"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "list_usage_events",
        "description": "List raw usage events with optional filters. Useful for auditing or debugging metered billing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "settled": {"type": "boolean", "description": "Filter by settled status"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    # ── v0.5.0: Service Marketplace Registry ────────────────────────────
    {
        "name": "register_service",
        "description": "Register a service on the agent marketplace. Providers publish their tools, APIs, or compute resources. Services start as DRAFT — call publish_service to make them discoverable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable service name"},
                "slug": {"type": "string", "description": "URL-safe unique identifier"},
                "provider_customer_id": {"type": "string", "description": "Customer ID of the provider (receives payments)"},
                "description": {"type": "string"},
                "category": {"type": "string", "default": "general", "description": "e.g. search, compute, data, translation"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Searchable tags"},
                "price_per_call": {"type": "number", "description": "USD cents per call/use"},
                "price_per_token": {"type": "number", "description": "USD cents per 1K tokens"},
                "price_per_second": {"type": "number", "description": "USD cents per second"},
                "free_tier_limit": {"type": "integer", "description": "Free calls before pricing applies"},
                "endpoint_url": {"type": "string", "description": "Where the service is hosted"},
                "mcp_server_url": {"type": "string", "description": "MCP server URL if service is MCP-native"},
                "api_schema": {"type": "object", "description": "JSON schema for service input/output"},
                "status": {"type": "string", "enum": [s.value for s in ServiceStatus], "default": "draft"},
            },
            "required": ["name", "slug", "provider_customer_id"],
        },
    },
    {
        "name": "publish_service",
        "description": "Publish a service — moves it from DRAFT to ACTIVE so agents can discover and purchase it.",
        "inputSchema": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
        },
    },
    {
        "name": "search_services",
        "description": "Search the marketplace for services. Returns matching services with in-line pricing, ratings, and endpoint info. This is the discovery entry point for agents looking for paid tools.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (matches name, description, tags, category)"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_services",
        "description": "Browse marketplace services with filters. Use category or tag to explore by type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "tag": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "get_service",
        "description": "Get full details for a specific marketplace service, including pricing, endpoint, and rating.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "slug": {"type": "string", "description": "Alternatively, look up by slug"},
            },
        },
    },
    {
        "name": "purchase_service",
        "description": "Purchase access to a marketplace service. Charges the customer and returns provisioning info (endpoint URL, API schema). This is the discover → pay → provision flow in one call. If the service has a price_per_call, that amount is charged; pass amount to override.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "customer_id": {"type": "string", "description": "Customer ID of the purchasing agent"},
                "amount": {"type": "number", "description": "Override charge amount (defaults to service price)"},
                "description": {"type": "string"},
            },
            "required": ["service_id", "customer_id"],
        },
    },
    {
        "name": "create_plan",
        "description": "Create a subscription plan for a marketplace service. Plans offer recurring billing with included quota.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "name": {"type": "string", "description": "Plan name (e.g. 'Pro', 'Starter')"},
                "price_cents": {"type": "integer", "description": "Recurring charge in cents"},
                "description": {"type": "string"},
                "billing_interval": {"type": "string", "enum": ["daily", "monthly", "yearly"], "default": "monthly"},
                "included_calls": {"type": "integer", "default": 0},
                "included_tokens": {"type": "integer", "default": 0},
                "features": {"type": "array", "items": {"type": "string"}},
                "trial_days": {"type": "integer", "default": 0},
            },
            "required": ["service_id", "name", "price_cents"],
        },
    },
    {
        "name": "subscribe_to_plan",
        "description": "Subscribe a customer to a subscription plan. Charges the recurring fee immediately and returns subscription details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "customer_id": {"type": "string"},
            },
            "required": ["plan_id", "customer_id"],
        },
    },
    {
        "name": "review_service",
        "description": "Leave a rating (1-5) and review for a marketplace service. Reviews are auto-verified if the reviewer has a successful payment for the service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "comment": {"type": "string"},
            },
            "required": ["service_id", "customer_id", "rating"],
        },
    },
    {
        "name": "list_service_reviews",
        "description": "List reviews for a marketplace service, optionally filtered by customer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
]


class MCPServer:
    """MCP server that exposes payment tools."""

    def __init__(self, engine: PaymentEngine | None = None):
        self.engine = engine or PaymentEngine()

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all available tools (MCP tools/list)."""
        return TOOL_DEFINITIONS

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle a tool call (MCP tools/call)."""
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return {"error": f"Unknown tool: {name}"}
            result = handler(arguments)
            return {"result": result}
        except Exception as exc:
            return {"error": str(exc)}

    # ── Tool Handlers ──────────────────────────────────────────────────

    def _tool_set_tool_price(self, args: dict) -> dict:
        currency = Currency(args.get("currency", "USD"))
        model = PricingModel(args.get("pricing_model", "per_use"))
        pricing = self.engine.set_price(
            tool_name=args["tool_name"],
            amount=args["amount"],
            currency=currency,
            pricing_model=model,
            free_tier_limit=args.get("free_tier_limit"),
            rate_limit=args.get("rate_limit"),
            description=args.get("description", ""),
        )
        return {"tool_name": pricing.tool_name, "price": pricing.price.display(), "pricing_model": pricing.price.pricing_model.value}

    def _tool_get_tool_price(self, args: dict) -> dict:
        pricing = self.engine.get_price(args["tool_name"])
        if not pricing:
            return {"error": "No pricing set for this tool", "tool_name": args["tool_name"]}
        return {
            "tool_name": pricing.tool_name,
            "amount": pricing.price.amount,
            "currency": pricing.price.currency.value,
            "display": pricing.price.display(),
            "pricing_model": pricing.price.pricing_model.value,
            "free_tier_limit": pricing.free_tier_limit,
            "enabled": pricing.enabled,
        }

    def _tool_list_tool_prices(self, args: dict) -> dict:
        prices = self.engine.list_prices()
        return {
            "count": len(prices),
            "prices": [
                {
                    "tool_name": p.tool_name,
                    "amount": p.price.amount,
                    "currency": p.price.currency.value,
                    "display": p.price.display(),
                    "pricing_model": p.price.pricing_model.value,
                    "free_tier_limit": p.free_tier_limit,
                }
                for p in prices
            ],
        }

    def _tool_create_customer(self, args: dict) -> dict:
        customer = self.engine.create_customer(
            name=args.get("name", ""),
            agent_id=args.get("agent_id"),
            wallet_address=args.get("wallet_address"),
            email=args.get("email"),
            metadata=args.get("metadata", {}),
        )
        return {"customer_id": customer.id, "balance": customer.balance, "name": customer.name}

    def _tool_get_customer(self, args: dict) -> dict:
        customer = self.engine.get_customer(args["customer_id"])
        if not customer:
            return {"error": "Customer not found"}
        return {
            "customer_id": customer.id,
            "name": customer.name,
            "agent_id": customer.agent_id,
            "wallet_address": customer.wallet_address,
            "balance": customer.balance,
            "created_at": customer.created_at.isoformat(),
        }

    def _tool_top_up_balance(self, args: dict) -> dict:
        customer = self.engine.top_up_balance(args["customer_id"], args["amount"])
        if not customer:
            return {"error": "Customer not found"}
        return {"customer_id": customer.id, "new_balance": customer.balance}

    def _tool_create_payment_intent(self, args: dict) -> dict:
        intent = self.engine.create_intent(
            customer_id=args["customer_id"],
            amount=args["amount"],
            currency=Currency(args.get("currency", "USD")),
            tool_name=args.get("tool_name"),
            description=args.get("description", ""),
            provider=PaymentProvider(args.get("provider", "internal")),
        )
        return {
            "intent_id": intent.id,
            "amount": intent.amount,
            "currency": intent.currency.value,
            "status": intent.status.value,
            "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
        }

    def _tool_charge(self, args: dict) -> dict:
        payment = self.engine.charge(
            customer_id=args["customer_id"],
            amount=args["amount"],
            currency=Currency(args.get("currency", "USD")),
            tool_name=args.get("tool_name"),
            description=args.get("description", ""),
            provider=PaymentProvider(args.get("provider", "internal")),
            metadata=args.get("metadata"),
        )
        return {
            "payment_id": payment.id,
            "status": payment.status.value,
            "amount": payment.amount,
            "currency": payment.currency.value,
            "failure_reason": payment.failure_reason,
        }

    def _tool_fulfill_intent(self, args: dict) -> dict:
        payment = self.engine.fulfill_intent(args["intent_id"])
        if payment is None:
            return {"error": "Intent not found, already fulfilled, or expired"}
        return {
            "payment_id": payment.id,
            "status": payment.status.value,
            "amount": payment.amount,
        }

    def _tool_get_payment(self, args: dict) -> dict:
        payment = self.engine.storage.get_payment(args["payment_id"])
        if not payment:
            return {"error": "Payment not found"}
        return {
            "payment_id": payment.id,
            "customer_id": payment.customer_id,
            "amount": payment.amount,
            "currency": payment.currency.value,
            "status": payment.status.value,
            "provider": payment.provider.value,
            "tool_name": payment.tool_name,
            "description": payment.description,
            "created_at": payment.created_at.isoformat(),
            "completed_at": payment.completed_at.isoformat() if payment.completed_at else None,
            "failure_reason": payment.failure_reason,
            "transaction_id": payment.provider_transaction_id,
            "refund_amount": payment.refund_amount,
        }

    def _tool_verify_payment(self, args: dict) -> dict:
        return self.engine.verify_payment(args["payment_id"])

    def _tool_refund_payment(self, args: dict) -> dict:
        refund = self.engine.refund(
            payment_id=args["payment_id"],
            amount=args.get("amount"),
            reason=args.get("reason", ""),
        )
        return {
            "refund_id": refund.id,
            "payment_id": refund.payment_id,
            "amount": refund.amount,
            "status": refund.status.value,
        }

    def _tool_get_receipt(self, args: dict) -> dict:
        receipt = self.engine.get_receipt(args["payment_id"])
        if not receipt:
            return {"error": "No completed payment found"}
        return receipt.model_dump(mode="json")

    def _tool_list_payments(self, args: dict) -> dict:
        status = PaymentStatus(args["status"]) if args.get("status") else None
        payments = self.engine.storage.list_payments(
            customer_id=args.get("customer_id"),
            status=status,
            limit=args.get("limit", 50),
        )
        return {
            "count": len(payments),
            "payments": [
                {
                    "payment_id": p.id,
                    "amount": p.amount,
                    "currency": p.currency.value,
                    "status": p.status.value,
                    "tool_name": p.tool_name,
                    "created_at": p.created_at.isoformat(),
                }
                for p in payments
            ],
        }

    def _tool_payment_summary(self, args: dict) -> dict:
        return self.engine.summary(args.get("customer_id"))

    def _tool_create_x402_response(self, args: dict) -> dict:
        req = self.engine.create_x402_requirements(
            amount=args["amount"],
            currency=Currency.USD,
            resource_url=args.get("resource_url", ""),
            description=args.get("description", ""),
            network=args.get("network", "base-sepolia"),
        )
        return req.model_dump()

    # ── v0.2.0: Escrow + Split handlers ───────────────────────────────

    def _tool_create_escrow(self, args: dict) -> dict:
        escrow = self.engine.create_escrow(
            payer_customer_id=args["payer_customer_id"],
            payee_customer_id=args["payee_customer_id"],
            amount=args["amount"],
            currency=Currency(args.get("currency", "USD")),
            task_description=args.get("task_description", ""),
            task_id=args.get("task_id"),
            tool_name=args.get("tool_name"),
            expires_in_seconds=args.get("expires_in_seconds"),
        )
        return {
            "escrow_id": escrow.id,
            "status": escrow.status.value,
            "amount": escrow.amount,
            "currency": escrow.currency.value,
            "payer": escrow.payer_customer_id,
            "payee": escrow.payee_customer_id,
            "task_description": escrow.task_description,
            "expires_at": escrow.expires_at.isoformat() if escrow.expires_at else None,
        }

    def _tool_release_escrow(self, args: dict) -> dict:
        escrow = self.engine.release_escrow(args["escrow_id"])
        if escrow is None:
            return {"error": "Escrow not found"}
        return {
            "escrow_id": escrow.id,
            "status": escrow.status.value,
            "released_at": escrow.released_at.isoformat() if escrow.released_at else None,
            "release_payment_id": escrow.release_payment_id,
        }

    def _tool_refund_escrow(self, args: dict) -> dict:
        escrow = self.engine.refund_escrow(args["escrow_id"], reason=args.get("reason", ""))
        if escrow is None:
            return {"error": "Escrow not found"}
        return {
            "escrow_id": escrow.id,
            "status": escrow.status.value,
            "refunded_at": escrow.refunded_at.isoformat() if escrow.refunded_at else None,
        }

    def _tool_get_escrow(self, args: dict) -> dict:
        escrow = self.engine.get_escrow(args["escrow_id"])
        if escrow is None:
            return {"error": "Escrow not found"}
        return {
            "escrow_id": escrow.id,
            "status": escrow.status.value,
            "amount": escrow.amount,
            "currency": escrow.currency.value,
            "payer_customer_id": escrow.payer_customer_id,
            "payee_customer_id": escrow.payee_customer_id,
            "task_description": escrow.task_description,
            "task_id": escrow.task_id,
            "created_at": escrow.created_at.isoformat(),
            "released_at": escrow.released_at.isoformat() if escrow.released_at else None,
            "expires_at": escrow.expires_at.isoformat() if escrow.expires_at else None,
            "payment_id": escrow.payment_id,
            "release_payment_id": escrow.release_payment_id,
            "dispute_reason": escrow.dispute_reason,
        }

    def _tool_list_escrows(self, args: dict) -> dict:
        escrows = self.engine.list_escrows(
            payer_id=args.get("payer_id"),
            payee_id=args.get("payee_id"),
            status=args.get("status"),
        )
        return {
            "count": len(escrows),
            "escrows": [
                {
                    "escrow_id": e.id,
                    "status": e.status.value,
                    "amount": e.amount,
                    "payer": e.payer_customer_id,
                    "payee": e.payee_customer_id,
                    "task_description": e.task_description,
                    "created_at": e.created_at.isoformat(),
                }
                for e in escrows
            ],
        }

    def _tool_create_split(self, args: dict) -> dict:
        split = self.engine.create_split(
            payer_customer_id=args["payer_customer_id"],
            shares=args["shares"],
            currency=Currency(args.get("currency", "USD")),
            source_payment_id=args.get("source_payment_id"),
            tool_name=args.get("tool_name"),
            description=args.get("description", ""),
        )
        return {
            "split_id": split.id,
            "status": split.status.value,
            "total_amount": split.total_amount,
            "currency": split.currency.value,
            "share_count": len(split.shares),
            "shares": [
                {
                    "customer_id": s.customer_id,
                    "amount": s.amount,
                    "percentage": s.percentage,
                    "label": s.label,
                }
                for s in split.shares
            ],
            "settlement_payment_ids": split.settlement_payment_ids,
        }

    def _tool_get_split(self, args: dict) -> dict:
        split = self.engine.get_split(args["split_id"])
        if split is None:
            return {"error": "Split not found"}
        return {
            "split_id": split.id,
            "status": split.status.value,
            "total_amount": split.total_amount,
            "currency": split.currency.value,
            "payer_customer_id": split.payer_customer_id,
            "shares": [
                {
                    "customer_id": s.customer_id,
                    "amount": s.amount,
                    "percentage": s.percentage,
                    "label": s.label,
                }
                for s in split.shares
            ],
            "created_at": split.created_at.isoformat(),
            "completed_at": split.completed_at.isoformat() if split.completed_at else None,
            "settlement_payment_ids": split.settlement_payment_ids,
        }

    # ── v0.3.0: x402 Billing Middleware handlers ─────────────────────

    def _tool_verify_x402_payment(self, args: dict) -> dict:
        """Verify an x402 payment header against the required amount."""
        from ..middleware import PaymentVerifier, _amount_to_atomic

        verifier = PaymentVerifier(
            merchant_wallet=args.get("merchant_wallet", self.engine.merchant_wallet),
        )
        requirements = X402PaymentRequirements(
            amount=_amount_to_atomic(args["amount"]),
            pay_to=args.get("merchant_wallet", self.engine.merchant_wallet),
            network=args.get("network", "base-sepolia"),
        )
        result = verifier.verify(args["payment_header"], requirements)
        return {
            "valid": result.valid,
            "reason": result.reason,
            "transaction_id": result.transaction_id,
        }

    def _tool_create_x402_middleware_config(self, args: dict) -> dict:
        """Generate x402 middleware pricing configuration."""
        from ..middleware import PricingRule, _amount_to_atomic

        merchant_wallet = args["merchant_wallet"]
        rules = args["rules"]

        config_rules = []
        for r in rules:
            config_rules.append({
                "method": r.get("method", "GET"),
                "path": r["path"],
                "amount_usd": r["amount"],
                "amount_atomic": _amount_to_atomic(r["amount"]),
                "description": r.get("description", f"Payment for {r['path']}"),
                "network": "base-sepolia",
            })

        return {
            "merchant_wallet": merchant_wallet,
            "rules": config_rules,
            "python_snippet": (
                "from mcp_payments.middleware import X402Middleware, PricingRule\n"
                "from starlette.applications import Starlette\n\n"
                f"pricing_rules = [\n"
                + "\n".join(
                    f"    PricingRule(method='{r['method']}', path='{r['path']}', "
                    f"amount={r['amount_usd']}, description='{r['description']}'),"
                    for r in config_rules
                )
                + "\n]\n\n"
                "app.add_middleware(\n"
                f"    X402Middleware,\n"
                f"    merchant_wallet='{merchant_wallet}',\n"
                "    pricing_rules=pricing_rules,\n"
                ")"
            ),
        }

    # ── v0.4.0: Usage Metering handlers ──────────────────────────────

    def _tool_record_usage(self, args: dict) -> dict:
        from ..models import UsageUnit

        unit = UsageUnit(args.get("unit", "calls"))
        event = self.engine.record_usage(
            customer_id=args["customer_id"],
            tool_name=args["tool_name"],
            unit=unit,
            quantity=args.get("quantity", 1),
            session_id=args.get("session_id"),
            request_id=args.get("request_id"),
            input_tokens=args.get("input_tokens"),
            output_tokens=args.get("output_tokens"),
            metadata=args.get("metadata"),
        )
        return {
            "event_id": event.id,
            "customer_id": event.customer_id,
            "tool_name": event.tool_name,
            "unit": event.unit.value,
            "quantity": event.quantity,
            "settled": event.settled,
            "timestamp": event.timestamp.isoformat(),
        }

    def _tool_get_usage_summary(self, args: dict) -> dict:
        summary = self.engine.get_usage_summary(
            customer_id=args["customer_id"],
            tool_name=args.get("tool_name"),
        )
        return {
            "customer_id": summary.customer_id,
            "tool_name": summary.tool_name,
            "period_start": summary.period_start.isoformat(),
            "period_end": summary.period_end.isoformat(),
            "total_events": summary.total_events,
            "total_by_unit": summary.total_by_unit,
            "estimated_cost": summary.estimated_cost,
            "currency": summary.currency.value,
            "settled_events": summary.settled_events,
            "unsettled_events": summary.unsettled_events,
        }

    def _tool_settle_usage(self, args: dict) -> dict:
        result = self.engine.settle_usage(
            customer_id=args["customer_id"],
            tool_name=args.get("tool_name"),
        )
        return {
            "customer_id": result.customer_id,
            "tool_name": result.tool_name,
            "period_start": result.period_start.isoformat(),
            "period_end": result.period_end.isoformat(),
            "events_settled": result.events_settled,
            "total_charged": result.total_charged,
            "currency": result.currency.value,
            "payment_ids": result.payment_ids,
            "breakdown": result.breakdown,
        }

    def _tool_list_usage_events(self, args: dict) -> dict:
        events = self.engine.list_usage_events(
            customer_id=args.get("customer_id"),
            tool_name=args.get("tool_name"),
            settled=args.get("settled"),
            limit=args.get("limit", 100),
        )
        return {
            "count": len(events),
            "events": [
                {
                    "event_id": e.id,
                    "customer_id": e.customer_id,
                    "tool_name": e.tool_name,
                    "unit": e.unit.value,
                    "quantity": e.quantity,
                    "settled": e.settled,
                    "session_id": e.session_id,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
        }

    # ── v0.5.0: Marketplace Handlers ──────────────────────────────────

    def _tool_register_service(self, args: dict) -> dict:
        status = ServiceStatus(args.get("status", "draft"))
        service = self.engine.register_service(
            name=args["name"],
            slug=args["slug"],
            provider_customer_id=args["provider_customer_id"],
            description=args.get("description", ""),
            category=args.get("category", "general"),
            tags=args.get("tags", []),
            price_per_call=args.get("price_per_call"),
            price_per_token=args.get("price_per_token"),
            price_per_second=args.get("price_per_second"),
            free_tier_limit=args.get("free_tier_limit"),
            endpoint_url=args.get("endpoint_url"),
            mcp_server_url=args.get("mcp_server_url"),
            api_schema=args.get("api_schema"),
            status=status,
        )
        return {
            "service_id": service.id,
            "name": service.name,
            "slug": service.slug,
            "status": service.status.value,
            "category": service.category,
        }

    def _tool_publish_service(self, args: dict) -> dict:
        service = self.engine.publish_service(args["service_id"])
        if service is None:
            return {"error": "Service not found"}
        return {
            "service_id": service.id,
            "name": service.name,
            "status": service.status.value,
            "message": f"Service '{service.name}' is now live and discoverable",
        }

    def _tool_search_services(self, args: dict) -> dict:
        services = self.engine.search_services(
            query=args["query"],
            limit=args.get("limit", 20),
        )
        return {
            "count": len(services),
            "query": args["query"],
            "services": [self._service_summary(s) for s in services],
        }

    def _tool_list_services(self, args: dict) -> dict:
        services = self.engine.list_services(
            category=args.get("category"),
            tag=args.get("tag"),
            status=args.get("status", "active"),
            limit=args.get("limit", 50),
        )
        return {
            "count": len(services),
            "services": [self._service_summary(s) for s in services],
        }

    def _tool_get_service(self, args: dict) -> dict:
        if args.get("service_id"):
            service = self.engine.get_service(args["service_id"])
        elif args.get("slug"):
            service = self.engine.get_service_by_slug(args["slug"])
        else:
            return {"error": "Provide service_id or slug"}
        if service is None:
            return {"error": "Service not found"}
        return self._service_detail(service)

    def _tool_purchase_service(self, args: dict) -> dict:
        result = self.engine.purchase_service(
            service_id=args["service_id"],
            customer_id=args["customer_id"],
            amount=args.get("amount"),
            description=args.get("description", ""),
        )
        return result

    def _tool_create_plan(self, args: dict) -> dict:
        plan = self.engine.create_plan(
            service_id=args["service_id"],
            name=args["name"],
            price_cents=args["price_cents"],
            description=args.get("description", ""),
            billing_interval=args.get("billing_interval", "monthly"),
            included_calls=args.get("included_calls", 0),
            included_tokens=args.get("included_tokens", 0),
            features=args.get("features", []),
            trial_days=args.get("trial_days", 0),
        )
        return {
            "plan_id": plan.id,
            "service_id": plan.service_id,
            "name": plan.name,
            "price_cents": plan.price_cents,
            "billing_interval": plan.billing_interval,
        }

    def _tool_subscribe_to_plan(self, args: dict) -> dict:
        result = self.engine.subscribe_to_plan(
            plan_id=args["plan_id"],
            customer_id=args["customer_id"],
        )
        return result

    def _tool_review_service(self, args: dict) -> dict:
        review = self.engine.review_service(
            service_id=args["service_id"],
            customer_id=args["customer_id"],
            rating=args["rating"],
            comment=args.get("comment", ""),
        )
        return {
            "review_id": review.id,
            "service_id": review.service_id,
            "rating": review.rating,
            "verified": review.verified,
        }

    def _tool_list_service_reviews(self, args: dict) -> dict:
        reviews = self.engine.list_reviews(
            service_id=args.get("service_id"),
            customer_id=args.get("customer_id"),
            limit=args.get("limit", 50),
        )
        return {
            "count": len(reviews),
            "reviews": [
                {
                    "review_id": r.id,
                    "service_id": r.service_id,
                    "customer_id": r.customer_id,
                    "rating": r.rating,
                    "comment": r.comment,
                    "verified": r.verified,
                    "created_at": r.created_at.isoformat(),
                }
                for r in reviews
            ],
        }

    # ── Marketplace helpers ───────────────────────────────────────────

    @staticmethod
    def _service_summary(s) -> dict:
        avg_rating = s.rating_sum / s.rating_count if s.rating_count > 0 else 0
        return {
            "service_id": s.id,
            "name": s.name,
            "slug": s.slug,
            "category": s.category,
            "description": s.description[:120],
            "price_per_call": s.price_per_call,
            "price_per_token": s.price_per_token,
            "free_tier_limit": s.free_tier_limit,
            "rating": round(avg_rating, 1),
            "rating_count": s.rating_count,
            "total_calls": s.total_calls,
            "mcp_server_url": s.mcp_server_url,
        }

    @staticmethod
    def _service_detail(s) -> dict:
        avg_rating = s.rating_sum / s.rating_count if s.rating_count > 0 else 0
        return {
            "service_id": s.id,
            "name": s.name,
            "slug": s.slug,
            "description": s.description,
            "category": s.category,
            "tags": s.tags,
            "provider_customer_id": s.provider_customer_id,
            "price_per_call": s.price_per_call,
            "price_per_token": s.price_per_token,
            "price_per_second": s.price_per_second,
            "free_tier_limit": s.free_tier_limit,
            "endpoint_url": s.endpoint_url,
            "mcp_server_url": s.mcp_server_url,
            "api_schema": s.api_schema,
            "status": s.status.value,
            "version": s.version,
            "rating": round(avg_rating, 1),
            "rating_count": s.rating_count,
            "total_calls": s.total_calls,
            "total_revenue": s.total_revenue,
            "homepage_url": s.homepage_url,
            "documentation_url": s.documentation_url,
            "created_at": s.created_at.isoformat(),
        }

