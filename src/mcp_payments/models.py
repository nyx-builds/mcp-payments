"""Core payment models and types."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class Currency(str, Enum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    USDC = "USDC"  # Circle stablecoin — x402 compatible
    USDT = "USDT"
    SOL = "SOL"
    ETH = "ETH"
    BTC = "BTC"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class PaymentProvider(str, Enum):
    INTERNAL = "internal"       # ledger-only, no external provider
    STRIPE = "stripe"           # fiat via Stripe
    X402 = "x402"               # Coinbase x402 HTTP-native crypto
    SOLANA = "solana"           # direct on-chain Solana transfer
    ETHEREUM = "ethereum"       # direct on-chain ETH transfer
    LIGHTNING = "lightning"     # Bitcoin Lightning Network


class PricingModel(str, Enum):
    FIXED = "fixed"
    PER_USE = "per_use"
    PER_TOKEN = "per_token"
    TIERED = "tiered"
    SUBSCRIPTION = "subscription"
    DYNAMIC = "dynamic"


class RefundStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# ── Core Models ────────────────────────────────────────────────────────────

class Price(BaseModel):
    """Pricing for a tool, resource, or service."""
    amount: float = Field(..., ge=0, description="Amount in the smallest currency unit (cents for fiat, base for crypto)")
    currency: Currency = Field(default=Currency.USD)
    pricing_model: PricingModel = Field(default=PricingModel.FIXED)
    description: str = Field(default="", description="Human-readable price description")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def display(self) -> str:
        """Human-readable price string."""
        symbols = {Currency.USD: "$", Currency.EUR: "€", Currency.GBP: "£", Currency.JPY: "¥"}
        if self.currency in symbols:
            sym = symbols[self.currency]
            return f"{sym}{self.amount:.2f}"
        return f"{self.amount:.4f} {self.currency.value}"


class ToolPricing(BaseModel):
    """Pricing schema for an MCP tool."""
    tool_name: str
    price: Price
    enabled: bool = Field(default=True)
    free_tier_limit: Optional[int] = Field(default=None, description="Number of free calls before pricing kicks in")
    rate_limit: Optional[int] = Field(default=None, description="Max calls per minute")


class Customer(BaseModel):
    """An agent or user that makes payments."""
    id: str = Field(default_factory=lambda: f"cus_{uuid.uuid4().hex[:24]}")
    name: str = Field(default="")
    agent_id: Optional[str] = Field(default=None, description="SAID/external agent identifier")
    wallet_address: Optional[str] = Field(default=None, description="Crypto wallet for x402/on-chain")
    email: Optional[str] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    balance: float = Field(default=0.0, description="Prepaid balance in USD cents")


class Payment(BaseModel):
    """A single payment transaction."""
    id: str = Field(default_factory=lambda: f"pay_{uuid.uuid4().hex[:24]}")
    customer_id: str
    amount: float = Field(..., ge=0)
    currency: Currency = Field(default=Currency.USD)
    status: PaymentStatus = Field(default=PaymentStatus.PENDING)
    provider: PaymentProvider = Field(default=PaymentProvider.INTERNAL)
    tool_name: Optional[str] = Field(default=None, description="MCP tool that triggered this payment")
    description: str = Field(default="")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None)
    failure_reason: Optional[str] = Field(default=None)
    provider_transaction_id: Optional[str] = Field(default=None, description="External transaction ID from Stripe/x402/etc")
    refund_amount: float = Field(default=0.0)


class Refund(BaseModel):
    """A refund for a payment."""
    id: str = Field(default_factory=lambda: f"ref_{uuid.uuid4().hex[:24]}")
    payment_id: str
    amount: float = Field(..., ge=0)
    currency: Currency = Field(default=Currency.USD)
    status: RefundStatus = Field(default=RefundStatus.PENDING)
    reason: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None)


class PaymentIntent(BaseModel):
    """Intent to pay — created before a payment is executed.

    For x402: the server responds with 402 + payment requirements.
    The client creates an intent, fulfills it, and retries.
    """
    id: str = Field(default_factory=lambda: f"pi_{uuid.uuid4().hex[:24]}")
    customer_id: str
    amount: float = Field(..., ge=0)
    currency: Currency = Field(default=Currency.USD)
    tool_name: Optional[str] = Field(default=None)
    description: str = Field(default="")
    status: PaymentStatus = Field(default=PaymentStatus.PENDING)
    provider: PaymentProvider = Field(default=PaymentProvider.INTERNAL)
    pricing: Optional[Price] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = Field(default=None)
    payment_id: Optional[str] = Field(default=None, description="Payment ID once fulfilled")


class X402PaymentRequirements(BaseModel):
    """x402 protocol payment requirements.

    Returned in HTTP 402 responses per Coinbase x402 spec.
    The agent reads this, determines how to pay, and retries with payment header.
    """
    scheme: str = Field(default="exact")
    network: str = Field(default="base-sepolia", description="Blockchain network")
    asset: str = Field(default="0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7235", description="USDC contract address")
    amount: str = Field(..., description="Amount in atomic units (wei)")
    pay_to: str = Field(..., description="Recipient wallet address")
    max_fee_required: str = Field(default="0")
    resource: str = Field(default="", description="URL that requires payment")
    description: str = Field(default="")
    mime_type: str = Field(default="application/json")
    output_schema: Optional[dict] = Field(default=None)


class X402PaymentHeader(BaseModel):
    """x402 payment fulfillment header sent by the agent.

    Contains the signed payment payload that proves payment was made.
    """
    x402_version: int = Field(default=1)
    kind: str = Field(default="verified")
    scheme: str = Field(default="exact")
    network: str = Field(default="base-sepolia")
    payload: dict[str, Any] = Field(default_factory=dict)


class PaymentReceipt(BaseModel):
    """Receipt for a completed payment."""
    payment_id: str
    customer_id: str
    amount: float
    currency: Currency
    provider: PaymentProvider
    tool_name: Optional[str] = None
    description: str = ""
    completed_at: datetime
    transaction_id: Optional[str] = None
    signature: Optional[str] = Field(default=None, description="Optional cryptographic signature for verification")


# ── v0.2.0: Escrow + Split Payments (agent-to-agent) ───────────────────────

class EscrowStatus(str, Enum):
    HELD = "held"
    RELEASED = "released"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


class SplitStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"


class Escrow(BaseModel):
    """Escrow holds funds until a task between agents completes.

    Agent A funds escrow → Agent B performs a task → Agent A releases
    (or Agent B disputes a non-release). Solves the trust gap in
    agent-to-agent transactions.
    """
    id: str = Field(default_factory=lambda: f"esc_{uuid.uuid4().hex[:24]}")
    payer_customer_id: str
    payee_customer_id: str
    amount: float = Field(..., ge=0)
    currency: Currency = Field(default=Currency.USD)
    status: EscrowStatus = Field(default=EscrowStatus.HELD)
    task_description: str = Field(default="", description="What the payee must do to earn release")
    task_id: Optional[str] = Field(default=None, description="External task / job ID")
    tool_name: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    released_at: Optional[datetime] = Field(default=None)
    refunded_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None, description="Auto-refund if not released by this time")
    payment_id: Optional[str] = Field(default=None, description="Funding payment ID")
    release_payment_id: Optional[str] = Field(default=None, description="Payment to payee on release")
    dispute_reason: Optional[str] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SplitShare(BaseModel):
    """A single recipient's share of a split payment."""
    customer_id: str
    amount: float = Field(..., ge=0)
    percentage: Optional[float] = Field(default=None, description="Optional percentage (0-100) for reference")
    label: str = Field(default="")


class SplitPayment(BaseModel):
    """Split a single charge across multiple recipients.

    e.g. Charge $10 → $7 to the tool provider, $2 to the platform fee,
    $1 to a referrer. One payment in, many ledger credits out.
    """
    id: str = Field(default_factory=lambda: f"spl_{uuid.uuid4().hex[:24]}")
    payer_customer_id: str
    total_amount: float = Field(..., ge=0)
    currency: Currency = Field(default=Currency.USD)
    shares: list[SplitShare] = Field(default_factory=list)
    status: SplitStatus = Field(default=SplitStatus.PENDING)
    source_payment_id: Optional[str] = Field(default=None, description="Original payment being split")
    tool_name: Optional[str] = Field(default=None)
    description: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None)
    settlement_payment_ids: list[str] = Field(default_factory=list, description="Payment IDs credited to each share")
    metadata: dict[str, Any] = Field(default_factory=dict)
