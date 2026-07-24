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


# ── v0.4.0: Usage Metering ─────────────────────────────────────────────────

class UsageUnit(str, Enum):
    """Units that usage can be measured in."""
    CALLS = "calls"            # per-use / per-invocation
    TOKENS = "tokens"          # LLM tokens (input + output)
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    SECONDS = "seconds"        # duration-based (compute time)
    REQUESTS = "requests"      # API requests
    BYTES = "bytes"            # data transfer
    CUSTOM = "custom"          # user-defined unit


class UsageEvent(BaseModel):
    """A single metered usage event from an agent.

    Recorded every time an agent calls a tool, consumes tokens, or uses
    a metered resource. Events accumulate in a billing period and are
    settled (charged) at period end or when explicitly triggered.
    """
    id: str = Field(default_factory=lambda: f"usage_{uuid.uuid4().hex[:24]}")
    customer_id: str
    tool_name: str
    unit: UsageUnit = Field(default=UsageUnit.CALLS)
    quantity: float = Field(..., ge=0, description="Amount consumed (e.g. 1500 tokens, 1 call, 30 seconds)")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = Field(default=None, description="Agent session that generated this event")
    request_id: Optional[str] = Field(default=None, description="Individual request/invocation ID")
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional breakdown for token-based pricing
    input_tokens: Optional[int] = Field(default=None, description="Input/prompt tokens (if unit=tokens)")
    output_tokens: Optional[int] = Field(default=None, description="Output/completion tokens (if unit=tokens)")
    settled: bool = Field(default=False, description="Whether this event has been included in a settlement charge")


class UsageSummary(BaseModel):
    """Aggregated usage for a customer/tool over a time window."""
    customer_id: str
    tool_name: Optional[str] = None
    period_start: datetime
    period_end: datetime
    total_events: int = 0
    total_by_unit: dict[str, float] = Field(default_factory=dict, description="Total quantity per unit type")
    estimated_cost: float = Field(default=0.0, description="Estimated cost based on current pricing")
    currency: Currency = Field(default=Currency.USD)
    unsettled_events: int = 0
    settled_events: int = 0


class SettlementResult(BaseModel):
    """Result of settling metered usage — charges applied."""
    customer_id: str
    tool_name: Optional[str] = None
    period_start: datetime
    period_end: datetime
    events_settled: int = 0
    total_charged: float = Field(default=0.0, ge=0)
    currency: Currency = Field(default=Currency.USD)
    payment_ids: list[str] = Field(default_factory=list)
    breakdown: dict[str, Any] = Field(default_factory=dict, description="Per-tool charge breakdown")


# ── v0.5.0: Service Marketplace Registry ────────────────────────────────────

class ServiceStatus(str, Enum):
    """Publication lifecycle of a marketplace service listing."""
    DRAFT = "draft"            # created but not publicly visible
    ACTIVE = "active"          # published and discoverable
    PAUSED = "paused"          # temporarily hidden
    DEPRECATED = "deprecated"  # still resolves but flagged
    DELISTED = "delisted"      # removed from marketplace


class ServiceListing(BaseModel):
    """A service listed on the agent marketplace.

    Providers publish their tools, APIs, or compute resources here.
    Agents discover services, check pricing, and purchase access —
    all within a single MCP payment flow.

    This is the registry entry that makes tool discovery + payment
    a unified experience: an agent searches, sees prices in-line,
    pays, and gets provisioning credentials in one call.
    """
    id: str = Field(default_factory=lambda: f"svc_{uuid.uuid4().hex[:24]}")
    name: str = Field(..., min_length=1, max_length=120, description="Human-readable service name")
    slug: str = Field(..., min_length=1, max_length=80, description="URL-safe identifier, unique in marketplace")
    description: str = Field(default="", max_length=2000)
    provider_customer_id: str = Field(..., description="Customer ID of the service provider (receives payments)")
    category: str = Field(default="general", description="e.g. 'search', 'compute', 'data', 'translation', 'vision'")
    tags: list[str] = Field(default_factory=list, description="Searchable tags for discovery")
    # Pricing — stored as structured data for in-line display during discovery
    price_per_call: Optional[float] = Field(default=None, ge=0, description="USD per call/use (cents)")
    price_per_token: Optional[float] = Field(default=None, ge=0, description="USD per 1K tokens (cents)")
    price_per_second: Optional[float] = Field(default=None, ge=0, description="USD per second (cents)")
    free_tier_limit: Optional[int] = Field(default=None, description="Free calls before pricing applies")
    # Technical
    endpoint_url: Optional[str] = Field(default=None, description="Where the service is hosted (for provisioning)")
    mcp_server_url: Optional[str] = Field(default=None, description="MCP server URL if service is MCP-native")
    api_schema: Optional[dict[str, Any]] = Field(default=None, description="JSON schema for service input/output")
    # Status
    status: ServiceStatus = Field(default=ServiceStatus.DRAFT)
    # Metrics (updated as agents use the service)
    total_calls: int = Field(default=0)
    total_revenue: float = Field(default=0.0, description="Lifetime revenue in cents")
    rating_sum: float = Field(default=0.0)
    rating_count: int = Field(default=0)
    # Metadata
    version: str = Field(default="1.0.0")
    homepage_url: Optional[str] = Field(default=None)
    documentation_url: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubscriptionPlan(BaseModel):
    """A subscription tier for a marketplace service.

    Services can offer flat recurring plans in addition to usage-based
    pricing. An agent subscribes and gets included quota each period.
    """
    id: str = Field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:24]}")
    service_id: str = Field(..., description="Which service this plan belongs to")
    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field(default="")
    price_cents: int = Field(..., ge=0, description="Recurring charge in cents")
    currency: Currency = Field(default=Currency.USD)
    billing_interval: str = Field(default="monthly", description="'monthly', 'daily', or 'yearly'")
    included_calls: int = Field(default=0, description="Calls included per period (0 = unlimited if price covers)")
    included_tokens: int = Field(default=0, description="Tokens included per period")
    # Features list for display
    features: list[str] = Field(default_factory=list)
    # Trial
    trial_days: int = Field(default=0, description="Free trial period in days")
    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ServiceReview(BaseModel):
    """A review/rating left by an agent that used a service."""
    id: str = Field(default_factory=lambda: f"rev_{uuid.uuid4().hex[:24]}")
    service_id: str
    customer_id: str = Field(..., description="The reviewing agent's customer ID")
    rating: int = Field(..., ge=1, le=5, description="1-5 star rating")
    comment: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Verification: was a successful payment made by this customer?
    verified: bool = Field(default=False, description="True if customer has a succeeded payment for this service")


# ── v0.6.0: Agent Spend Controls (budgets, limits, pre-auth) ──────────────

class SpendWindow(str, Enum):
    """Time window for a spend limit."""
    PER_TRANSACTION = "per_transaction"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class SpendPolicy(BaseModel):
    """A spend policy that controls how much an agent can spend.

    Prevents runaway agents from draining budgets. Policies can limit:
    - Per-transaction amount (block charges above a threshold)
    - Rolling window totals (daily/weekly/monthly caps)
    - Tool-level restrictions (allow/deny specific tools)
    - Required pre-authorization (hold funds before a charge)

    Policies are scoped per customer. Multiple policies per customer are
    supported — the most restrictive applicable policy wins.
    """
    id: str = Field(default_factory=lambda: f"pol_{uuid.uuid4().hex[:24]}")
    customer_id: str = Field(..., description="Customer this policy applies to")
    name: str = Field(default="default", description="Human-readable policy name")
    # Limits
    max_per_transaction: Optional[float] = Field(default=None, ge=0, description="Max amount per single charge (cents)")
    daily_limit: Optional[float] = Field(default=None, ge=0, description="Max total spend per day (cents)")
    weekly_limit: Optional[float] = Field(default=None, ge=0, description="Max total spend per week (cents)")
    monthly_limit: Optional[float] = Field(default=None, ge=0, description="Max total spend per month (cents)")
    # Tool restrictions
    allowed_tools: Optional[list[str]] = Field(default=None, description="Whitelist of tools the agent can pay for (None = all allowed)")
    blocked_tools: list[str] = Field(default_factory=list, description="Blacklist of tools the agent cannot pay for")
    # Rate limiting
    max_transactions_per_hour: Optional[int] = Field(default=None, ge=0, description="Max number of charges per hour")
    # State
    enabled: bool = Field(default=True)
    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthorizationStatus(str, Enum):
    """Result of a spend authorization check."""
    APPROVED = "approved"
    DENIED_POLICY_DISABLED = "denied_policy_disabled"
    DENIED_OVER_PER_TRANSACTION = "denied_over_per_transaction"
    DENIED_OVER_DAILY_LIMIT = "denied_over_daily_limit"
    DENIED_OVER_WEEKLY_LIMIT = "denied_over_weekly_limit"
    DENIED_OVER_MONTHLY_LIMIT = "denied_over_monthly_limit"
    DENIED_TOOL_BLOCKED = "denied_tool_blocked"
    DENIED_TOOL_NOT_ALLOWED = "denied_tool_not_allowed"
    DENIED_INSUFFICIENT_BALANCE = "denied_insufficient_balance"
    DENIED_RATE_LIMITED = "denied_rate_limited"


class AuthorizationResult(BaseModel):
    """Result of checking whether a charge is allowed under spend policies."""
    authorized: bool
    status: AuthorizationStatus
    amount: float
    customer_id: str
    tool_name: Optional[str] = None
    policy_id: Optional[str] = None
    reason: str = Field(default="")
    # Current spend context (for transparency)
    daily_spend: float = Field(default=0.0)
    daily_limit: Optional[float] = None
    monthly_spend: float = Field(default=0.0)
    monthly_limit: Optional[float] = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SpendReport(BaseModel):
    """Aggregated spend report for a customer over a time period."""
    customer_id: str
    period_start: datetime
    period_end: datetime
    total_spend: float = Field(default=0.0, description="Total succeeded charges in cents")
    total_transactions: int = 0
    total_refunded: float = Field(default=0.0)
    net_spend: float = Field(default=0.0)
    by_tool: dict[str, float] = Field(default_factory=dict, description="Spend per tool name")
    by_day: dict[str, float] = Field(default_factory=dict, description="Spend per date (ISO date string)")
    average_transaction: float = Field(default=0.0)
    largest_transaction: float = Field(default=0.0)
    policies_applied: list[str] = Field(default_factory=list, description="Policy IDs that affected this customer")
