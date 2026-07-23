# mcp-payments

**Payment execution layer for AI agents.** MCP server + CLI. Charge, refund, escrow, split payments, x402 billing middleware, and verify transactions for autonomous agents.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/nyx-builds/mcp-payments/actions/workflows/ci.yml/badge.svg)](https://github.com/nyx-builds/mcp-payments/actions/workflows/ci.yml)
[![319 tests](https://img.shields.io/badge/tests-319%20passing-brightgreen.svg)](#)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)
[![x402](https://img.shields.io/badge/x402-ready-orange.svg)](https://github.com/x402-protocol)
[![Version](https://img.shields.io/badge/version-0.5.0-blue.svg)](#)

## Why?

The MCP ecosystem has **no payment layer**. Agents can call tools, read resources, and generate prompts — but they can't pay for premium tools, get charged for their usage, or verify payments programmatically. As agents start transacting with each other, they also need **trust mechanisms** — escrow for task completion, split payments for multi-party settlement, and **billing middleware** to monetize MCP endpoints via the x402 HTTP 402 protocol.

**mcp-payments** fills this gap:
- 🔧 **MCP-native** — pricing is part of tool discovery
- 💸 **Multi-provider** — internal ledger, Stripe (fiat), x402 (crypto), on-chain
- 🛒 **Service Marketplace Registry (NEW v0.5.0)** — agents discover, purchase, and provision paid services in one flow
- 📊 **Usage metering (v0.4.0)** — record, aggregate, and settle metered billing (per-call, per-token, per-second)
- 🔗 **x402 billing middleware (v0.3.0)** — enforce HTTP 402 payments on any ASGI app
- 📊 **Full lifecycle** — pricing → intent → charge → verify → refund → receipt
- 🔒 **Escrow** — hold funds until a task between agents completes
- ✂️ **Split payments** — distribute one charge to multiple recipients
- 🏪 **Suite-compatible** — works with [agent-invoice](https://github.com/nyx-builds/agent-invoice), [agent-ledger](https://github.com/nyx-builds/agent-ledger), [agent-budget](https://github.com/nyx-builds/agent-budget)

## Quick Start

```bash
pip install mcp-payments
```

### Set tool pricing
```bash
mcp-payments price my-premium-tool 50 --model per_use --free-tier 10
# 10 free calls, then $0.50 per use
```

### Register a customer and charge
```bash
mcp-payments register --name "My Agent" --wallet 0xABC...
mcp-payments top-up cus_xxx 10000  # $100.00 in cents
mcp-payments charge cus_xxx 50 --tool my-premium-tool
```

### Generate x402 payment requirements
```bash
mcp-payments x402 0.01 --resource-url https://api.example.com/premium \
  --merchant-wallet 0x123...
```

## Service Marketplace Registry (NEW v0.5.0)

The first **unified discovery + payment** layer for AI agents. Providers list services; agents search, see in-line pricing, purchase, and get provisioning credentials — all through one MCP server.

This closes the loop that competitors are building piecemeal: Rail402 does discovery, piprail does x402 SDK, agent-discovery-mcp does ERC-8004. mcp-payments unifies all of it.

```python
from mcp_payments.engine import PaymentEngine

engine = PaymentEngine()

# 1. Provider registers a service
svc = engine.register_service(
    name="Web Search API",
    slug="web-search",
    provider_customer_id=provider.id,
    description="Full-text web search for agents",
    category="search",
    tags=["search", "web", "research"],
    price_per_call=5,  # 5 cents per query
    free_tier_limit=10,
    endpoint_url="https://api.example.com/v1/search",
    mcp_server_url="https://mcp.example.com/search",
)
engine.publish_service(svc.id)

# 2. Agent discovers via search
results = engine.search_services("web search")
# → Returns services with in-line pricing, ratings, endpoint info

# 3. Agent purchases — discover → pay → provision in one call
access = engine.purchase_service(svc.id, customer_id=buyer.id)
# → {access_granted: True, endpoint_url: "...", payment_id: "pay_...", ...}

# 4. Agent leaves a verified review
engine.review_service(svc.id, customer_id=buyer.id, rating=5, comment="Fast and accurate")
# → verified=True (auto-checked against payment history)

# 5. Subscription plans
plan = engine.create_plan(svc.id, "Pro", price_cents=1000, included_calls=1000)
engine.subscribe_to_plan(plan.id, customer_id=buyer.id)
```

**MCP tools added (10 new):** `register_service`, `publish_service`, `search_services`, `list_services`, `get_service`, `purchase_service`, `create_plan`, `subscribe_to_plan`, `review_service`, `list_service_reviews`

## x402 Billing Middleware (v0.3.0)

Enforce HTTP 402 payments on any ASGI app (FastAPI, Starlette). Agents that request a paid endpoint receive a `402 Payment Required` with x402 payment requirements. When they retry with a valid `X-PAYMENT` header, the middleware verifies the payment and returns the resource.

```python
from mcp_payments.middleware import X402Middleware, PricingRule
from fastapi import FastAPI

app = FastAPI()

# Define pricing rules for different endpoints
pricing_rules = [
    PricingRule(method="GET", path="/api/premium", amount=0.01, description="Premium data"),
    PricingRule(method="POST", path="/api/analyze", amount=0.05, description="AI analysis"),
]

app.add_middleware(
    X402Middleware,
    merchant_wallet="0x742d35Cc6634C0532925a3b844Bc9e7595f0bAe1",
    pricing_rules=pricing_rules,
)
```

**How it works:**

1. Agent requests `GET /api/premium`
2. Middleware returns `402 Payment Required`:
```json
{
  "x402Version": 1,
  "accepts": [{
    "scheme": "exact",
    "network": "base-sepolia",
    "asset": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7235",
    "amount": "10000",
    "pay_to": "0x742d...",
    "resource": "/api/premium",
    "description": "Premium data"
  }]
}
```
3. Agent pays (on-chain USDC) and retries with `X-PAYMENT` header
4. Middleware verifies payment → returns resource with `X-PAYMENT-RESPONSE` settlement confirmation

**Features:**
- Static or dynamic pricing per endpoint
- Optional API key bypass via `check_fn`
- HMAC signature verification for signed payments
- Facilitator client for on-chain verification (or simulate mode for testing)
- Path prefix stripping for versioned APIs

## MCP Tools (25 available)

| Tool | Description |
|------|-------------|
| `set_tool_price` | Set pricing for an MCP tool |
| `get_tool_price` | Check pricing for a tool |
| `list_tool_prices` | List all tool pricing |
| `create_customer` | Register a customer/agent |
| `get_customer` | Look up a customer |
| `top_up_balance` | Add prepaid balance |
| `create_payment_intent` | Create intent (x402 compatible) |
| `charge` | Charge a customer immediately |
| `fulfill_intent` | Execute a payment intent |
| `get_payment` | Get payment details |
| `verify_payment` | Verify payment validity |
| `refund_payment` | Refund (full or partial) |
| `get_receipt` | Generate signed receipt |
| `list_payments` | List with filters |
| `payment_summary` | Analytics and revenue |
| `create_x402_response` | Generate HTTP 402 requirements |
| `create_escrow` | Hold funds until task completes |
| `release_escrow` | Release escrow to payee |
| `refund_escrow` | Refund escrow to payer |
| `get_escrow` | Check escrow status |
| `list_escrows` | List/filter escrows |
| `create_split` | Split payment to multiple recipients |
| `get_split` | Check split payment status |
| `verify_x402_payment` | **NEW** Verify an x402 payment header |
| `create_x402_middleware_config` | **NEW** Generate middleware pricing config |

## Escrow & Split Payments (v0.2.0)

### Escrow — agent-to-agent trust

Agent A funds escrow → Agent B performs a task → Agent A releases the funds.
If the task isn't done, Agent A refunds. If escrow expires, funds auto-refund.

```python
from mcp_payments.engine import PaymentEngine

engine = PaymentEngine()
payer = engine.create_customer(name="Payer Agent")
payee = engine.create_customer(name="Worker Agent")
engine.top_up_balance(payer.id, 10000)

# Hold $5 in escrow for a task
escrow = engine.create_escrow(
    payer_customer_id=payer.id,
    payee_customer_id=payee.id,
    amount=500,
    task_description="Summarize 10 articles",
    expires_in_seconds=86400,  # auto-refund in 24h
)

# When the task is done, release
engine.release_escrow(escrow.id)
# Or if not done: engine.refund_escrow(escrow.id, reason="not completed")
```

### Split payments — multi-recipient settlement

Distribute one charge across multiple recipients — perfect for marketplaces,
platform fees, and revenue sharing.

```python
# Charge $10, split $7 to provider, $2 platform, $1 referrer
split = engine.create_split(
    payer_customer_id=payer.id,
    shares=[
        {"customer_id": provider.id, "amount": 7.00, "label": "provider"},
        {"customer_id": platform.id, "amount": 2.00, "label": "platform_fee"},
        {"customer_id": referrer.id, "amount": 1.00, "label": "referral"},
    ],
)
# Each recipient is credited instantly (auto_settle=True by default)
```

## Pricing Models

- **fixed** — One-time payment
- **per_use** — Charge per tool invocation
- **per_token** — Charge per token processed
- **tiered** — Volume-based pricing
- **subscription** — Recurring payment
- **dynamic** — Market-driven pricing

## Payment Providers

| Provider | Type | Status |
|----------|------|--------|
| `internal` | Ledger-only (prepaid balance) | ✅ Production-ready |
| `x402` | Coinbase HTTP-native crypto | ✅ Protocol support + middleware |
| `stripe` | Fiat via Stripe | 🔧 Stub (requires API keys) |
| `solana` | On-chain SOL | 🔧 Stub (requires RPC) |
| `ethereum` | On-chain ETH | 🔧 Stub (requires RPC) |
| `lightning` | Bitcoin Lightning | 🔧 Stub |

## Architecture

```
mcp-payments/
├── src/mcp_payments/
│   ├── models.py       # Pydantic models (Payment, Customer, Price, Escrow, etc.)
│   ├── engine.py       # Payment processing engine (charge, escrow, split)
│   ├── middleware.py   # x402 billing middleware (NEW v0.3.0)
│   ├── storage.py      # JSON-backed storage (swap to SQL for production)
│   ├── server/         # MCP server (25 tools)
│   └── cli/            # CLI interface
├── tests/              # Comprehensive test suite (319 tests)
└── docs/               # Documentation
```

## Suite

This is part of the agent financial infrastructure suite:

| Package | Role |
|---------|------|
| **mcp-payments** | Payment execution (this repo) |
| [agent-invoice](https://github.com/nyx-builds/agent-invoice) | Billing & invoicing |
| [agent-ledger](https://github.com/nyx-builds/agent-ledger) | Double-entry accounting |
| [agent-budget](https://github.com/nyx-builds/agent-budget) | Budget tracking |

## License

MIT
