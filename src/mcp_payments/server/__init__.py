"""MCP server — exposes payment tools via Model Context Protocol."""
from __future__ import annotations

import json
from typing import Any

from ..engine import PaymentEngine
from ..models import Currency, PaymentProvider, PaymentStatus, PricingModel


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
