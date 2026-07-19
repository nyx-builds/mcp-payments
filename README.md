# mcp-payments

**Payment execution layer for AI agents.** MCP server + CLI. Create, charge, and verify payments with x402 protocol support.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/nyx-builds/mcp-payments/actions/workflows/ci.yml/badge.svg)](https://github.com/nyx-builds/mcp-payments/actions/workflows/ci.yml)
[![131 tests](https://img.shields.io/badge/tests-131%20passing-brightgreen.svg)](#)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)
[![x402](https://img.shields.io/badge/x402-ready-orange.svg)](https://github.com/x402-protocol)
[![Version](https://img.shields.io/badge/version-0.1.1-blue.svg)](#)

## Why?

The MCP ecosystem has **no payment layer**. Agents can call tools, read resources, and generate prompts — but they can't pay for premium tools, get charged for their usage, or verify payments programmatically.

**mcp-payments** fills this gap:
- 🔧 **MCP-native** — pricing is part of tool discovery
- 💸 **Multi-provider** — internal ledger, Stripe (fiat), x402 (crypto), on-chain
- 🔗 **x402-ready** — generate HTTP 402 payment requirements for Coinbase's payment protocol
- 📊 **Full lifecycle** — pricing → intent → charge → verify → refund → receipt
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

## MCP Tools (16 available)

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
| `x402` | Coinbase HTTP-native crypto | ✅ Protocol support |
| `stripe` | Fiat via Stripe | 🔧 Stub (requires API keys) |
| `solana` | On-chain SOL | 🔧 Stub (requires RPC) |
| `ethereum` | On-chain ETH | 🔧 Stub (requires RPC) |
| `lightning` | Bitcoin Lightning | 🔧 Stub |

## Architecture

```
mcp-payments/
├── src/mcp_payments/
│   ├── models.py       # Pydantic models (Payment, Customer, Price, etc.)
│   ├── engine.py       # Payment processing engine
│   ├── storage.py      # JSON-backed storage (swap to SQL for production)
│   ├── server/         # MCP server (16 tools)
│   └── cli/            # CLI interface
├── tests/              # Comprehensive test suite
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
