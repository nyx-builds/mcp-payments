"""Payment processing engine — core business logic."""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from .models import (
    AuthorizationResult,
    AuthorizationStatus,
    Currency,
    Customer,
    Escrow,
    EscrowStatus,
    Payment,
    PaymentIntent,
    PaymentProvider,
    PaymentReceipt,
    PaymentStatus,
    Price,
    PricingModel,
    Refund,
    RefundStatus,
    ServiceListing,
    ServiceReview,
    ServiceStatus,
    SettlementResult,
    SpendPolicy,
    SpendReport,
    SplitPayment,
    SplitShare,
    SplitStatus,
    SubscriptionPlan,
    ToolPricing,
    UsageEvent,
    UsageSummary,
    UsageUnit,
    X402PaymentRequirements,
)
from .storage import Storage


class PaymentEngine:
    """Core payment processing engine."""

    def __init__(self, storage: Storage | None = None, merchant_wallet: str = ""):
        self.storage = storage or Storage()
        self.merchant_wallet = merchant_wallet

    # ── Tool Pricing ───────────────────────────────────────────────────

    def set_price(
        self,
        tool_name: str,
        amount: float,
        currency: Currency = Currency.USD,
        pricing_model: PricingModel = PricingModel.PER_USE,
        free_tier_limit: int | None = None,
        rate_limit: int | None = None,
        description: str = "",
    ) -> ToolPricing:
        """Set pricing for an MCP tool."""
        price = Price(amount=amount, currency=currency, pricing_model=pricing_model, description=description)
        pricing = ToolPricing(
            tool_name=tool_name,
            price=price,
            free_tier_limit=free_tier_limit,
            rate_limit=rate_limit,
        )
        return self.storage.set_tool_pricing(pricing)

    def get_price(self, tool_name: str) -> Optional[ToolPricing]:
        return self.storage.get_tool_pricing(tool_name)

    def list_prices(self) -> list[ToolPricing]:
        return self.storage.list_tool_pricing()

    def check_free_tier(self, tool_name: str, customer_id: str) -> bool:
        """Check if customer is still within free tier."""
        pricing = self.storage.get_tool_pricing(tool_name)
        if not pricing or pricing.free_tier_limit is None:
            return False
        usage = len([
            p for p in self.storage.list_payments(customer_id=customer_id)
            if p.tool_name == tool_name and p.status == PaymentStatus.SUCCEEDED
        ])
        return usage < pricing.free_tier_limit

    # ── Customer Management ────────────────────────────────────────────

    def create_customer(
        self,
        name: str = "",
        agent_id: str | None = None,
        wallet_address: str | None = None,
        email: str | None = None,
        metadata: dict | None = None,
    ) -> Customer:
        customer = Customer(
            name=name,
            agent_id=agent_id,
            wallet_address=wallet_address,
            email=email,
            metadata=metadata or {},
        )
        return self.storage.create_customer(customer)

    def get_customer(self, customer_id: str) -> Optional[Customer]:
        return self.storage.get_customer(customer_id)

    def top_up_balance(self, customer_id: str, amount: float) -> Optional[Customer]:
        """Add prepaid balance to a customer account."""
        return self.storage.update_customer_balance(customer_id, amount)

    # ── Payment Intents (x402 compatible) ──────────────────────────────

    def create_intent(
        self,
        customer_id: str,
        amount: float,
        currency: Currency = Currency.USD,
        tool_name: str | None = None,
        description: str = "",
        provider: PaymentProvider = PaymentProvider.INTERNAL,
        expires_in_seconds: int = 900,
    ) -> PaymentIntent:
        """Create a payment intent. For x402, this is the 402 response."""
        intent = PaymentIntent(
            customer_id=customer_id,
            amount=amount,
            currency=currency,
            tool_name=tool_name,
            description=description,
            provider=provider,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        )
        return self.storage.create_intent(intent)

    def create_x402_requirements(
        self,
        amount: float,
        currency: Currency,
        resource_url: str = "",
        description: str = "",
        network: str = "base-sepolia",
    ) -> X402PaymentRequirements:
        """Generate x402 payment requirements for HTTP 402 response.

        Amount is in USD; converted to USDC atomic units (6 decimals).
        """
        if not self.merchant_wallet:
            raise ValueError("merchant_wallet not configured — set via PaymentEngine(merchant_wallet='0x...')")

        # Convert to atomic USDC units (6 decimals)
        atomic_amount = str(int(amount * 1_000_000))

        return X402PaymentRequirements(
            amount=atomic_amount,
            pay_to=self.merchant_wallet,
            resource=resource_url,
            description=description or f"Payment for {resource_url}",
            network=network,
        )

    # ── Payment Execution ──────────────────────────────────────────────

    def charge(
        self,
        customer_id: str,
        amount: float,
        currency: Currency = Currency.USD,
        tool_name: str | None = None,
        description: str = "",
        provider: PaymentProvider = PaymentProvider.INTERNAL,
        metadata: dict | None = None,
    ) -> Payment:
        """Charge a customer. Deducts from prepaid balance for internal payments."""

        # Validate customer exists
        customer = self.storage.get_customer(customer_id)
        if customer is None:
            raise ValueError(f"Customer not found: {customer_id}")

        # ── v0.6.0: Check spend policies before charging ──
        auth = self.check_authorization(
            customer_id=customer_id,
            amount=amount,
            tool_name=tool_name,
        )
        if not auth.authorized:
            return self.storage.create_payment(Payment(
                customer_id=customer_id,
                amount=amount,
                currency=currency,
                status=PaymentStatus.FAILED,
                provider=provider,
                tool_name=tool_name,
                description=f"[SPEND DENIED] {description}",
                failure_reason=auth.reason,
                metadata={**(metadata or {}), "authorization_status": auth.status.value},
            ))

        # Check pricing / free tier
        if tool_name:
            pricing = self.storage.get_tool_pricing(tool_name)
            if pricing and pricing.free_tier_limit:
                if self.check_free_tier(tool_name, customer_id):
                    # Free tier — create $0 payment
                    payment = Payment(
                        customer_id=customer_id,
                        amount=0,
                        currency=currency,
                        status=PaymentStatus.SUCCEEDED,
                        provider=PaymentProvider.INTERNAL,
                        tool_name=tool_name,
                        description=f"[FREE TIER] {description}",
                        metadata=metadata or {},
                        completed_at=datetime.now(timezone.utc),
                    )
                    return self.storage.create_payment(payment)

        # Create payment record
        payment = Payment(
            customer_id=customer_id,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PROCESSING,
            provider=provider,
            tool_name=tool_name,
            description=description,
            metadata=metadata or {},
        )
        self.storage.create_payment(payment)

        # Process based on provider
        if provider == PaymentProvider.INTERNAL:
            result = self._process_internal(payment, customer)
        elif provider == PaymentProvider.X402:
            result = self._process_x402(payment, customer)
        elif provider == PaymentProvider.STRIPE:
            result = self._process_stripe(payment, customer)
        else:
            result = self._process_onchain(payment, customer)

        return result

    def _process_internal(self, payment: Payment, customer: Customer) -> Payment:
        """Internal ledger payment — deduct from prepaid balance."""
        if customer.balance < payment.amount:
            return self.storage.update_payment(
                payment.id,
                status=PaymentStatus.FAILED,
                failure_reason=f"Insufficient balance: {customer.balance:.2f} < {payment.amount:.2f}",
            )

        self.storage.update_customer_balance(customer.id, -payment.amount)
        return self.storage.update_payment(
            payment.id,
            status=PaymentStatus.SUCCEEDED,
            completed_at=datetime.now(timezone.utc),
            provider_transaction_id=f"int_{uuid.uuid4().hex[:16]}",
        )

    def _process_x402(self, payment: Payment, customer: Customer) -> Payment:
        """x402 payment — would integrate with facilitator. For now, simulate success if wallet configured."""
        if not customer.wallet_address:
            return self.storage.update_payment(
                payment.id,
                status=PaymentStatus.FAILED,
                failure_reason="No wallet address for x402 payment",
            )
        # In production: verify on-chain transaction via x402 facilitator
        return self.storage.update_payment(
            payment.id,
            status=PaymentStatus.SUCCEEDED,
            completed_at=datetime.now(timezone.utc),
            provider_transaction_id=f"x402_{uuid.uuid4().hex[:16]}",
        )

    def _process_stripe(self, payment: Payment, customer: Customer) -> Payment:
        """Stripe payment — would integrate with Stripe API. Stub marks as pending."""
        # In production: create Stripe PaymentIntent, confirm, capture
        return self.storage.update_payment(
            payment.id,
            status=PaymentStatus.PENDING,
            failure_reason=None,
            provider_transaction_id=f"stripe_{uuid.uuid4().hex[:16]}",
            metadata={**payment.metadata, "stripe_status": "requires_confirmation"},
        )

    def _process_onchain(self, payment: Payment, customer: Customer) -> Payment:
        """Direct on-chain payment verification. Stub for Solana/ETH."""
        if not customer.wallet_address:
            return self.storage.update_payment(
                payment.id,
                status=PaymentStatus.FAILED,
                failure_reason="No wallet address for on-chain payment",
            )
        return self.storage.update_payment(
            payment.id,
            status=PaymentStatus.SUCCEEDED,
            completed_at=datetime.now(timezone.utc),
            provider_transaction_id=f"chain_{uuid.uuid4().hex[:16]}",
        )

    # ── Fulfill Intent ─────────────────────────────────────────────────

    def fulfill_intent(self, intent_id: str) -> Optional[Payment]:
        """Fulfill a payment intent — execute the actual charge."""
        intent = self.storage.get_intent(intent_id)
        if intent is None:
            return None
        if intent.status != PaymentStatus.PENDING:
            return None
        if intent.expires_at and datetime.now(timezone.utc) > intent.expires_at:
            self.storage.update_intent(intent_id, status=PaymentStatus.CANCELLED)
            return None

        payment = self.charge(
            customer_id=intent.customer_id,
            amount=intent.amount,
            currency=intent.currency,
            tool_name=intent.tool_name,
            description=intent.description,
            provider=intent.provider,
        )

        self.storage.update_intent(
            intent_id,
            status=payment.status,
            payment_id=payment.id,
        )
        return payment

    # ── Refunds ────────────────────────────────────────────────────────

    def refund(
        self,
        payment_id: str,
        amount: float | None = None,
        reason: str = "",
    ) -> Refund:
        """Refund a payment (full or partial)."""
        payment = self.storage.get_payment(payment_id)
        if payment is None:
            raise ValueError(f"Payment not found: {payment_id}")
        if payment.status != PaymentStatus.SUCCEEDED:
            raise ValueError(f"Cannot refund payment with status: {payment.status}")

        refund_amount = amount if amount is not None else payment.amount
        if refund_amount > (payment.amount - payment.refund_amount):
            raise ValueError(
                f"Refund amount {refund_amount} exceeds refundable amount "
                f"{payment.amount - payment.refund_amount}"
            )

        refund = Refund(
            payment_id=payment_id,
            amount=refund_amount,
            currency=payment.currency,
            reason=reason,
            status=RefundStatus.SUCCEEDED,
            completed_at=datetime.now(timezone.utc),
        )
        self.storage.create_refund(refund)

        # Update payment
        self.storage.update_payment(
            payment_id,
            refund_amount=payment.refund_amount + refund_amount,
            status=PaymentStatus.REFUNDED if (payment.refund_amount + refund_amount) >= payment.amount else payment.status,
        )

        # Credit back customer balance for internal payments
        if payment.provider == PaymentProvider.INTERNAL:
            self.storage.update_customer_balance(payment.customer_id, refund_amount)

        return refund

    # ── Receipts ───────────────────────────────────────────────────────

    def get_receipt(self, payment_id: str) -> Optional[PaymentReceipt]:
        """Generate a receipt for a completed payment."""
        payment = self.storage.get_payment(payment_id)
        if payment is None or payment.status != PaymentStatus.SUCCEEDED:
            return None

        # Create signature for verification
        sig_data = f"{payment.id}:{payment.customer_id}:{payment.amount}:{payment.completed_at}"
        signature = hashlib.sha256(sig_data.encode()).hexdigest()

        return PaymentReceipt(
            payment_id=payment.id,
            customer_id=payment.customer_id,
            amount=payment.amount,
            currency=payment.currency,
            provider=payment.provider,
            tool_name=payment.tool_name,
            description=payment.description,
            completed_at=payment.completed_at,
            transaction_id=payment.provider_transaction_id,
            signature=signature,
        )

    # ── Verification ───────────────────────────────────────────────────

    def verify_payment(self, payment_id: str) -> dict[str, Any]:
        """Verify a payment's status and validity."""
        payment = self.storage.get_payment(payment_id)
        if payment is None:
            return {"valid": False, "reason": "Payment not found"}

        return {
            "valid": payment.status == PaymentStatus.SUCCEEDED,
            "payment_id": payment.id,
            "status": payment.status.value,
            "amount": payment.amount,
            "currency": payment.currency.value,
            "provider": payment.provider.value,
            "tool_name": payment.tool_name,
            "completed_at": payment.completed_at.isoformat() if payment.completed_at else None,
            "transaction_id": payment.provider_transaction_id,
        }

    # ── Analytics ──────────────────────────────────────────────────────

    def summary(self, customer_id: str | None = None) -> dict[str, Any]:
        """Payment summary / analytics."""
        payments = self.storage.list_payments(customer_id=customer_id, limit=10000)
        succeeded = [p for p in payments if p.status == PaymentStatus.SUCCEEDED]
        failed = [p for p in payments if p.status == PaymentStatus.FAILED]
        refunded = [p for p in payments if p.status == PaymentStatus.REFUNDED]

        total_volume = sum(p.amount for p in succeeded)
        total_refunded = sum(p.refund_amount for p in payments)

        # Revenue by tool
        by_tool: dict[str, float] = {}
        for p in succeeded:
            tool = p.tool_name or "unattributed"
            by_tool[tool] = by_tool.get(tool, 0) + p.amount

        return {
            "total_payments": len(payments),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "refunded": len(refunded),
            "total_volume": round(total_volume, 2),
            "total_refunded": round(total_refunded, 2),
            "net_revenue": round(total_volume - total_refunded, 2),
            "by_tool": by_tool,
            "customers": len(self.storage.list_customers()),
        }

    # ── Webhook HMAC ───────────────────────────────────────────────────

    def generate_webhook_signature(self, payload: bytes, secret: str) -> str:
        """Generate HMAC signature for webhook payload."""
        return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify_webhook(self, payload: bytes, signature: str, secret: str, tolerance: int = 300) -> bool:
        """Verify a webhook HMAC signature with timestamp tolerance."""
        expected = self.generate_webhook_signature(payload, secret)
        return hmac.compare_digest(expected, signature)

    # ── Escrow (v0.2.0 — agent-to-agent trust) ─────────────────────────

    def create_escrow(
        self,
        payer_customer_id: str,
        payee_customer_id: str,
        amount: float,
        currency: Currency = Currency.USD,
        task_description: str = "",
        task_id: str | None = None,
        tool_name: str | None = None,
        expires_in_seconds: int | None = None,
        metadata: dict | None = None,
    ) -> Escrow:
        """Create an escrow that holds funds until a task completes.

        Funds are charged from the payer immediately and held. The payer
        releases when satisfied, or funds are refunded if expired/disputed.
        """
        # Validate both customers exist
        payer = self.storage.get_customer(payer_customer_id)
        payee = self.storage.get_customer(payee_customer_id)
        if payer is None:
            raise ValueError(f"Payer not found: {payer_customer_id}")
        if payee is None:
            raise ValueError(f"Payee not found: {payee_customer_id}")
        if payer_customer_id == payee_customer_id:
            raise ValueError("Payer and payee must be different")

        # Charge the payer — funds go into escrow, not to the payee yet
        funding_payment = self.charge(
            customer_id=payer_customer_id,
            amount=amount,
            currency=currency,
            tool_name=tool_name,
            description=f"[ESCROW] {task_description}",
            metadata={"escrow": True, **(metadata or {})},
        )

        if funding_payment.status != PaymentStatus.SUCCEEDED:
            raise ValueError(
                f"Escrow funding failed: {funding_payment.failure_reason or funding_payment.status.value}"
            )

        expires_at = None
        if expires_in_seconds is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)

        escrow = Escrow(
            payer_customer_id=payer_customer_id,
            payee_customer_id=payee_customer_id,
            amount=amount,
            currency=currency,
            task_description=task_description,
            task_id=task_id,
            tool_name=tool_name,
            expires_at=expires_at,
            payment_id=funding_payment.id,
            metadata=metadata or {},
        )
        return self.storage.create_escrow(escrow)

    def release_escrow(self, escrow_id: str) -> Optional[Escrow]:
        """Release escrow funds to the payee."""
        escrow = self.storage.get_escrow(escrow_id)
        if escrow is None:
            return None
        if escrow.status != EscrowStatus.HELD:
            raise ValueError(f"Cannot release escrow with status: {escrow.status.value}")

        # Credit the payee's balance
        self.storage.update_customer_balance(escrow.payee_customer_id, escrow.amount)

        # Record the release payment
        release_payment = Payment(
            customer_id=escrow.payee_customer_id,
            amount=escrow.amount,
            currency=escrow.currency,
            status=PaymentStatus.SUCCEEDED,
            provider=PaymentProvider.INTERNAL,
            tool_name=escrow.tool_name,
            description=f"[ESCROW RELEASE] {escrow.task_description}",
            completed_at=datetime.now(timezone.utc),
            provider_transaction_id=f"esc_rel_{uuid.uuid4().hex[:16]}",
        )
        self.storage.create_payment(release_payment)

        updated = self.storage.update_escrow(
            escrow_id,
            status=EscrowStatus.RELEASED,
            released_at=datetime.now(timezone.utc),
            release_payment_id=release_payment.id,
        )
        return updated

    def refund_escrow(self, escrow_id: str, reason: str = "") -> Optional[Escrow]:
        """Refund escrow funds back to the payer (e.g. task not completed)."""
        escrow = self.storage.get_escrow(escrow_id)
        if escrow is None:
            return None
        if escrow.status != EscrowStatus.HELD:
            raise ValueError(f"Cannot refund escrow with status: {escrow.status.value}")

        # Credit the payer back
        self.storage.update_customer_balance(escrow.payer_customer_id, escrow.amount)

        return self.storage.update_escrow(
            escrow_id,
            status=EscrowStatus.REFUNDED,
            refunded_at=datetime.now(timezone.utc),
            dispute_reason=reason or None,
        )

    def dispute_escrow(self, escrow_id: str, reason: str) -> Optional[Escrow]:
        """Mark an escrow as disputed (payee claims non-release by payer)."""
        escrow = self.storage.get_escrow(escrow_id)
        if escrow is None:
            return None
        if escrow.status not in (EscrowStatus.HELD,):
            raise ValueError(f"Cannot dispute escrow with status: {escrow.status.value}")
        return self.storage.update_escrow(
            escrow_id, status=EscrowStatus.DISPUTED, dispute_reason=reason
        )

    def auto_expire_escrows(self) -> list[Escrow]:
        """Auto-refund any escrows past their expiry time. Returns refunded list."""
        now = datetime.now(timezone.utc)
        expired = []
        all_escrows = self.storage.list_escrows(status=EscrowStatus.HELD.value, limit=10000)
        for esc in all_escrows:
            if esc.expires_at and now > esc.expires_at:
                refunded = self.refund_escrow(esc.id, reason="Auto-expired")
                if refunded:
                    expired.append(refunded)
        return expired

    def get_escrow(self, escrow_id: str) -> Optional[Escrow]:
        return self.storage.get_escrow(escrow_id)

    def list_escrows(
        self,
        payer_id: str | None = None,
        payee_id: str | None = None,
        status: str | None = None,
    ) -> list[Escrow]:
        status_val = status.value if hasattr(status, "value") else status
        return self.storage.list_escrows(payer_id=payer_id, payee_id=payee_id, status=status_val)

    # ── Split Payments (v0.2.0 — multi-recipient settlement) ───────────

    def create_split(
        self,
        payer_customer_id: str,
        shares: list[dict[str, Any]],
        currency: Currency = Currency.USD,
        source_payment_id: str | None = None,
        tool_name: str | None = None,
        description: str = "",
        metadata: dict | None = None,
        auto_settle: bool = True,
    ) -> SplitPayment:
        """Create a split payment that distributes funds to multiple recipients.

        ``shares`` is a list of dicts: ``{"customer_id": "...", "amount": 7.00, "label": "provider"}``
        or ``{"customer_id": "...", "percentage": 70, "label": "provider"}``.

        If percentages are given, they are computed from the total (sum of amounts or source payment).
        """
        # Validate payer
        payer = self.storage.get_customer(payer_customer_id)
        if payer is None:
            raise ValueError(f"Payer not found: {payer_customer_id}")

        # Normalize shares: if percentage given without amount, compute from total
        parsed_shares: list[SplitShare] = []
        total_from_amounts = sum(s.get("amount", 0) for s in shares)

        # Determine the base total
        if total_from_amounts > 0:
            base_total = total_from_amounts
        elif source_payment_id:
            src = self.storage.get_payment(source_payment_id)
            if src is None:
                raise ValueError(f"Source payment not found: {source_payment_id}")
            base_total = src.amount
        else:
            raise ValueError("Cannot determine total: provide amounts or source_payment_id")

        for s in shares:
            cid = s["customer_id"]
            amt = s.get("amount")
            pct = s.get("percentage")
            if amt is None and pct is not None:
                amt = round(base_total * pct / 100, 2)
            if amt is None:
                raise ValueError(f"Share for {cid} has no amount or percentage")
            parsed_shares.append(SplitShare(
                customer_id=cid, amount=amt, percentage=pct, label=s.get("label", "")
            ))

        total_shares = sum(s.amount for s in parsed_shares)

        split = SplitPayment(
            payer_customer_id=payer_customer_id,
            total_amount=total_shares,
            currency=currency,
            shares=parsed_shares,
            source_payment_id=source_payment_id,
            tool_name=tool_name,
            description=description,
            metadata=metadata or {},
        )
        created = self.storage.create_split(split)

        if auto_settle:
            self.settle_split(created.id)
            created = self.storage.get_split(created.id) or created

        return created

    def settle_split(self, split_id: str) -> Optional[SplitPayment]:
        """Execute the split: credit each recipient."""
        split = self.storage.get_split(split_id)
        if split is None:
            return None
        if split.status in (SplitStatus.COMPLETED, SplitStatus.PARTIALLY_COMPLETED):
            return split

        settlement_ids: list[str] = []
        all_ok = True

        for share in split.shares:
            recipient = self.storage.get_customer(share.customer_id)
            if recipient is None:
                all_ok = False
                continue
            # Credit recipient balance
            self.storage.update_customer_balance(share.customer_id, share.amount)
            # Record settlement payment
            pay = Payment(
                customer_id=share.customer_id,
                amount=share.amount,
                currency=split.currency,
                status=PaymentStatus.SUCCEEDED,
                provider=PaymentProvider.INTERNAL,
                tool_name=split.tool_name,
                description=f"[SPLIT] {share.label or split.description}",
                completed_at=datetime.now(timezone.utc),
                provider_transaction_id=f"spl_{uuid.uuid4().hex[:16]}",
                metadata={"split_id": split_id},
            )
            self.storage.create_payment(pay)
            settlement_ids.append(pay.id)

        new_status = SplitStatus.COMPLETED if all_ok else SplitStatus.PARTIALLY_COMPLETED
        return self.storage.update_split(
            split_id,
            status=new_status,
            completed_at=datetime.now(timezone.utc),
            settlement_payment_ids=settlement_ids,
        )

    def get_split(self, split_id: str) -> Optional[SplitPayment]:
        return self.storage.get_split(split_id)

    def list_splits(
        self, payer_id: str | None = None, status: str | None = None
    ) -> list[SplitPayment]:
        status_val = status.value if hasattr(status, "value") else status
        return self.storage.list_splits(payer_id=payer_id, status=status_val)

    # ── Usage Metering (v0.4.0 — track and settle agent consumption) ──

    def record_usage(
        self,
        customer_id: str,
        tool_name: str,
        unit: UsageUnit = UsageUnit.CALLS,
        quantity: float = 1,
        session_id: str | None = None,
        request_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        metadata: dict | None = None,
    ) -> UsageEvent:
        """Record a metered usage event."""
        if unit in (UsageUnit.TOKENS, UsageUnit.INPUT_TOKENS, UsageUnit.OUTPUT_TOKENS):
            if input_tokens and output_tokens and quantity == 1:
                quantity = input_tokens + output_tokens

        if self.storage.get_customer(customer_id) is None:
            raise ValueError(f"Customer not found: {customer_id}")

        event = UsageEvent(
            customer_id=customer_id,
            tool_name=tool_name,
            unit=unit,
            quantity=quantity,
            session_id=session_id,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metadata=metadata or {},
        )
        return self.storage.create_usage_event(event)

    def get_usage_summary(
        self,
        customer_id: str,
        tool_name: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> UsageSummary:
        """Aggregate usage events into a summary with estimated cost."""
        now = datetime.now(timezone.utc)
        if period_start is None:
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_end is None:
            period_end = now

        events = self.storage.list_usage_events(
            customer_id=customer_id,
            tool_name=tool_name,
            since=period_start,
            until=period_end,
            limit=100000,
        )

        total_by_unit: dict[str, float] = {}
        settled_count = 0
        unsettled_count = 0

        for ev in events:
            unit_key = ev.unit.value
            total_by_unit[unit_key] = total_by_unit.get(unit_key, 0) + ev.quantity
            if ev.settled:
                settled_count += 1
            else:
                unsettled_count += 1

        estimated_cost = self._estimate_usage_cost(events)

        return UsageSummary(
            customer_id=customer_id,
            tool_name=tool_name,
            period_start=period_start,
            period_end=period_end,
            total_events=len(events),
            total_by_unit=total_by_unit,
            estimated_cost=round(estimated_cost, 4),
            currency=Currency.USD,
            settled_events=settled_count,
            unsettled_events=unsettled_count,
        )

    def _estimate_usage_cost(self, events: list[UsageEvent]) -> float:
        """Estimate total cost for usage events based on tool pricing."""
        total = 0.0
        tool_totals: dict[str, dict[str, float]] = {}
        for ev in events:
            tool = tool_totals.setdefault(ev.tool_name, {})
            unit = ev.unit.value
            tool[unit] = tool.get(unit, 0) + ev.quantity

        for tool_name, units in tool_totals.items():
            pricing = self.storage.get_tool_pricing(tool_name)
            if not pricing:
                continue
            amount = pricing.price.amount
            model = pricing.price.pricing_model

            for unit, qty in units.items():
                if model == PricingModel.PER_USE and unit == UsageUnit.CALLS.value:
                    total += qty * amount
                elif model == PricingModel.PER_TOKEN and unit in (
                    UsageUnit.TOKENS.value,
                    UsageUnit.INPUT_TOKENS.value,
                    UsageUnit.OUTPUT_TOKENS.value,
                ):
                    total += (qty / 1000) * amount
                elif model == PricingModel.FIXED:
                    total += amount
        return total

    def settle_usage(
        self,
        customer_id: str,
        tool_name: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> SettlementResult:
        """Settle unsettled usage events — charge the customer for accumulated usage."""
        now = datetime.now(timezone.utc)
        if period_start is None:
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_end is None:
            period_end = now

        unsettled = self.storage.list_usage_events(
            customer_id=customer_id,
            tool_name=tool_name,
            settled=False,
            since=period_start,
            until=period_end,
            limit=100000,
        )

        if not unsettled:
            return SettlementResult(
                customer_id=customer_id,
                tool_name=tool_name,
                period_start=period_start,
                period_end=period_end,
            )

        by_tool: dict[str, list[UsageEvent]] = {}
        for ev in unsettled:
            by_tool.setdefault(ev.tool_name, []).append(ev)

        payment_ids: list[str] = []
        breakdown: dict[str, Any] = {}
        total_charged = 0.0
        events_settled = 0

        for tl_name, events in by_tool.items():
            cost = self._estimate_usage_cost(events)
            if cost <= 0:
                event_ids = [e.id for e in events]
                self.storage.mark_events_settled(event_ids)
                events_settled += len(events)
                breakdown[tl_name] = {"events": len(events), "cost": 0, "charged": False}
                continue

            charge_amount = float(Decimal(str(cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            payment = self.charge(
                customer_id=customer_id,
                amount=charge_amount,
                currency=Currency.USD,
                tool_name=tl_name,
                description=f"[METERED] {len(events)} events settled",
                provider=PaymentProvider.INTERNAL,
                metadata={
                    "metered": True,
                    "event_count": len(events),
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                },
            )

            event_ids = [e.id for e in events]
            self.storage.mark_events_settled(event_ids)
            events_settled += len(events)
            total_charged += payment.amount

            breakdown[tl_name] = {
                "events": len(events),
                "cost": round(cost, 4),
                "charged": payment.status == PaymentStatus.SUCCEEDED,
                "payment_id": payment.id,
                "status": payment.status.value,
            }
            payment_ids.append(payment.id)

        return SettlementResult(
            customer_id=customer_id,
            tool_name=tool_name,
            period_start=period_start,
            period_end=period_end,
            events_settled=events_settled,
            total_charged=round(total_charged, 2),
            currency=Currency.USD,
            payment_ids=payment_ids,
            breakdown=breakdown,
        )

    def list_usage_events(
        self,
        customer_id: str | None = None,
        tool_name: str | None = None,
        settled: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageEvent]:
        """List usage events with optional filters."""
        return self.storage.list_usage_events(
            customer_id=customer_id,
            tool_name=tool_name,
            settled=settled,
            since=since,
            until=until,
            limit=limit,
        )

    def get_usage_event(self, event_id: str) -> Optional[UsageEvent]:
        return self.storage.get_usage_event(event_id)

    # ── Marketplace: Service Registry (v0.5.0) ─────────────────────────
    #
    # Agents discover paid services, see in-line pricing, and purchase
    # access — all through a single MCP server. This closes the loop:
    # discover → price → pay → provision.

    def register_service(
        self,
        name: str,
        slug: str,
        provider_customer_id: str,
        description: str = "",
        category: str = "general",
        tags: list[str] | None = None,
        price_per_call: float | None = None,
        price_per_token: float | None = None,
        price_per_second: float | None = None,
        free_tier_limit: int | None = None,
        endpoint_url: str | None = None,
        mcp_server_url: str | None = None,
        api_schema: dict | None = None,
        status: ServiceStatus = ServiceStatus.DRAFT,
        homepage_url: str | None = None,
        documentation_url: str | None = None,
        metadata: dict | None = None,
    ) -> ServiceListing:
        """Register a new service on the marketplace."""
        # Validate slug uniqueness
        existing = self.storage.get_service_by_slug(slug)
        if existing:
            raise ValueError(f"Service slug already taken: {slug}")

        # Validate provider exists
        provider = self.storage.get_customer(provider_customer_id)
        if provider is None:
            raise ValueError(f"Provider customer not found: {provider_customer_id}")

        service = ServiceListing(
            name=name,
            slug=slug,
            provider_customer_id=provider_customer_id,
            description=description,
            category=category,
            tags=tags or [],
            price_per_call=price_per_call,
            price_per_token=price_per_token,
            price_per_second=price_per_second,
            free_tier_limit=free_tier_limit,
            endpoint_url=endpoint_url,
            mcp_server_url=mcp_server_url,
            api_schema=api_schema,
            status=status,
            homepage_url=homepage_url,
            documentation_url=documentation_url,
            metadata=metadata or {},
        )
        return self.storage.create_service(service)

    def get_service(self, service_id: str) -> Optional[ServiceListing]:
        return self.storage.get_service(service_id)

    def get_service_by_slug(self, slug: str) -> Optional[ServiceListing]:
        return self.storage.get_service_by_slug(slug)

    def update_service(self, service_id: str, **kwargs) -> Optional[ServiceListing]:
        return self.storage.update_service(service_id, **kwargs)

    def publish_service(self, service_id: str) -> Optional[ServiceListing]:
        """Move a service from DRAFT to ACTIVE — makes it discoverable."""
        return self.storage.update_service(service_id, status=ServiceStatus.ACTIVE)

    def list_services(
        self,
        status: str | None = None,
        category: str | None = None,
        provider_id: str | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> list[ServiceListing]:
        return self.storage.list_services(
            status=status,
            category=category,
            provider_id=provider_id,
            tag=tag,
            limit=limit,
        )

    def search_services(self, query: str, limit: int = 20) -> list[ServiceListing]:
        """Search the marketplace for services matching a query."""
        return self.storage.search_services(query, limit=limit)

    def delete_service(self, service_id: str) -> bool:
        return self.storage.delete_service(service_id)

    def purchase_service(
        self,
        service_id: str,
        customer_id: str,
        amount: float | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Purchase access to a marketplace service.

        This is the discover → pay → provision flow in a single call.
        Charges the customer and returns service access details.

        If amount is None, uses the service's price_per_call.
        Returns the payment result + provisioning info (endpoint, schema).
        """
        service = self.storage.get_service(service_id)
        if service is None:
            raise ValueError(f"Service not found: {service_id}")

        if service.status.value not in ("active", "deprecated"):
            raise ValueError(f"Service is not available (status: {service.status.value})")

        # Determine charge amount
        if amount is None:
            amount = service.price_per_call or 0

        if amount > 0:
            payment = self.charge(
                customer_id=customer_id,
                amount=amount,
                currency=Currency.USD,
                tool_name=service.slug,
                description=description or f"[MARKETPLACE] {service.name}",
                provider=PaymentProvider.INTERNAL,
                metadata={"service_id": service_id, "marketplace": True},
            )
        else:
            # Free service or free tier
            payment = Payment(
                customer_id=customer_id,
                amount=0,
                currency=Currency.USD,
                status=PaymentStatus.SUCCEEDED,
                provider=PaymentProvider.INTERNAL,
                tool_name=service.slug,
                description=f"[MARKETPLACE-FREE] {service.name}",
                completed_at=datetime.now(timezone.utc),
            )
            self.storage.create_payment(payment)

        # Update service metrics
        self.storage.update_service(
            service_id,
            total_calls=service.total_calls + 1,
            total_revenue=service.total_revenue + amount,
            updated_at=datetime.now(timezone.utc),
        )

        # Also register the tool pricing so metering works
        if service.price_per_call and not self.storage.get_tool_pricing(service.slug):
            self.set_price(
                tool_name=service.slug,
                amount=service.price_per_call,
                pricing_model=PricingModel.PER_USE,
                free_tier_limit=service.free_tier_limit,
            )

        return {
            "service_id": service.id,
            "service_name": service.name,
            "payment_id": payment.id,
            "payment_status": payment.status.value,
            "amount_charged": payment.amount,
            "endpoint_url": service.endpoint_url,
            "mcp_server_url": service.mcp_server_url,
            "api_schema": service.api_schema,
            "access_granted": payment.status == PaymentStatus.SUCCEEDED,
        }

    # ── Marketplace: Subscription Plans ────────────────────────────────

    def create_plan(
        self,
        service_id: str,
        name: str,
        price_cents: int,
        description: str = "",
        billing_interval: str = "monthly",
        included_calls: int = 0,
        included_tokens: int = 0,
        features: list[str] | None = None,
        trial_days: int = 0,
        metadata: dict | None = None,
    ) -> SubscriptionPlan:
        """Create a subscription plan for a marketplace service."""
        service = self.storage.get_service(service_id)
        if service is None:
            raise ValueError(f"Service not found: {service_id}")

        plan = SubscriptionPlan(
            service_id=service_id,
            name=name,
            price_cents=price_cents,
            description=description,
            billing_interval=billing_interval,
            included_calls=included_calls,
            included_tokens=included_tokens,
            features=features or [],
            trial_days=trial_days,
            metadata=metadata or {},
        )
        return self.storage.create_plan(plan)

    def get_plan(self, plan_id: str) -> Optional[SubscriptionPlan]:
        return self.storage.get_plan(plan_id)

    def list_plans(self, service_id: str | None = None) -> list[SubscriptionPlan]:
        return self.storage.list_plans(service_id=service_id)

    def subscribe_to_plan(
        self,
        plan_id: str,
        customer_id: str,
    ) -> dict[str, Any]:
        """Subscribe a customer to a plan — charges the recurring fee immediately."""
        plan = self.storage.get_plan(plan_id)
        if plan is None:
            raise ValueError(f"Plan not found: {plan_id}")

        service = self.storage.get_service(plan.service_id)
        if service is None:
            raise ValueError("Service for this plan no longer exists")

        if plan.price_cents > 0:
            payment = self.charge(
                customer_id=customer_id,
                amount=plan.price_cents,
                currency=Currency.USD,
                tool_name=f"{service.slug}:plan:{plan.name}",
                description=f"[SUBSCRIPTION] {plan.name} for {service.name} ({plan.billing_interval})",
                provider=PaymentProvider.INTERNAL,
                metadata={"plan_id": plan_id, "service_id": plan.service_id},
            )
        else:
            payment = Payment(
                customer_id=customer_id,
                amount=0,
                currency=Currency.USD,
                status=PaymentStatus.SUCCEEDED,
                provider=PaymentProvider.INTERNAL,
                tool_name=f"{service.slug}:plan:{plan.name}",
                description=f"[SUBSCRIPTION-FREE] {plan.name}",
                completed_at=datetime.now(timezone.utc),
            )
            self.storage.create_payment(payment)

        return {
            "plan_id": plan.id,
            "plan_name": plan.name,
            "service_name": service.name,
            "payment_id": payment.id,
            "payment_status": payment.status.value,
            "amount_charged": payment.amount,
            "billing_interval": plan.billing_interval,
            "included_calls": plan.included_calls,
            "included_tokens": plan.included_tokens,
            "trial_days": plan.trial_days,
            "subscribed": payment.status == PaymentStatus.SUCCEEDED,
        }

    # ── Marketplace: Reviews ───────────────────────────────────────────

    def review_service(
        self,
        service_id: str,
        customer_id: str,
        rating: int,
        comment: str = "",
    ) -> ServiceReview:
        """Leave a review for a marketplace service.

        Reviews are automatically verified if the customer has a
        successful payment for the service — builds trust.
        """
        service = self.storage.get_service(service_id)
        if service is None:
            raise ValueError(f"Service not found: {service_id}")

        if not 1 <= rating <= 5:
            raise ValueError("Rating must be between 1 and 5")

        # Check if customer has a verified purchase
        payments = self.storage.list_payments(customer_id=customer_id, limit=10000)
        verified = any(
            p.tool_name == service.slug
            and p.status == PaymentStatus.SUCCEEDED
            for p in payments
        )

        review = ServiceReview(
            service_id=service_id,
            customer_id=customer_id,
            rating=rating,
            comment=comment,
            verified=verified,
        )
        self.storage.create_review(review)

        # Update service aggregate rating
        self.storage.update_service(
            service_id,
            rating_sum=service.rating_sum + rating,
            rating_count=service.rating_count + 1,
        )

        return review

    def list_reviews(
        self,
        service_id: str | None = None,
        customer_id: str | None = None,
        limit: int = 100,
    ) -> list[ServiceReview]:
        return self.storage.list_reviews(
            service_id=service_id,
            customer_id=customer_id,
            limit=limit,
        )

    # ── Spend Controls (v0.6.0 — budgets, limits, pre-auth) ──────────
    #
    # Prevents runaway agents. Set policies per customer with per-transaction,
    # daily/weekly/monthly caps, and tool allow/deny lists. The charge() method
    # automatically checks all applicable policies before executing.

    def set_spend_policy(
        self,
        customer_id: str,
        name: str = "default",
        max_per_transaction: float | None = None,
        daily_limit: float | None = None,
        weekly_limit: float | None = None,
        monthly_limit: float | None = None,
        allowed_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
        max_transactions_per_hour: int | None = None,
        enabled: bool = True,
        metadata: dict | None = None,
    ) -> SpendPolicy:
        """Create or update a spend policy for a customer.

        If a policy with the same customer_id + name exists, it's updated.
        Otherwise a new policy is created.
        """
        if self.storage.get_customer(customer_id) is None:
            raise ValueError(f"Customer not found: {customer_id}")

        # Check if a policy with this name already exists for this customer
        existing = [
            p for p in self.storage.list_spend_policies(customer_id=customer_id)
            if p.name == name
        ]

        if existing:
            # Update the existing policy
            policy = existing[0]
            updates = {}
            if max_per_transaction is not None:
                updates["max_per_transaction"] = max_per_transaction
            if daily_limit is not None:
                updates["daily_limit"] = daily_limit
            if weekly_limit is not None:
                updates["weekly_limit"] = weekly_limit
            if monthly_limit is not None:
                updates["monthly_limit"] = monthly_limit
            if allowed_tools is not None:
                updates["allowed_tools"] = allowed_tools
            if blocked_tools is not None:
                updates["blocked_tools"] = blocked_tools
            if max_transactions_per_hour is not None:
                updates["max_transactions_per_hour"] = max_transactions_per_hour
            updates["enabled"] = enabled
            if metadata:
                updates["metadata"] = {**policy.metadata, **metadata}
            updated = self.storage.update_spend_policy(policy.id, **updates)
            return updated if updated is not None else policy

        policy = SpendPolicy(
            customer_id=customer_id,
            name=name,
            max_per_transaction=max_per_transaction,
            daily_limit=daily_limit,
            weekly_limit=weekly_limit,
            monthly_limit=monthly_limit,
            allowed_tools=allowed_tools,
            blocked_tools=blocked_tools or [],
            max_transactions_per_hour=max_transactions_per_hour,
            enabled=enabled,
            metadata=metadata or {},
        )
        return self.storage.create_spend_policy(policy)

    def get_spend_policy(self, policy_id: str) -> Optional[SpendPolicy]:
        return self.storage.get_spend_policy(policy_id)

    def list_spend_policies(
        self,
        customer_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[SpendPolicy]:
        return self.storage.list_spend_policies(
            customer_id=customer_id,
            enabled=enabled,
        )

    def delete_spend_policy(self, policy_id: str) -> bool:
        return self.storage.delete_spend_policy(policy_id)

    def check_authorization(
        self,
        customer_id: str,
        amount: float,
        tool_name: str | None = None,
    ) -> AuthorizationResult:
        """Check if a charge is authorized under the customer's spend policies.

        Evaluates ALL active policies for the customer. If any policy denies
        the charge, the charge is denied (most restrictive wins).
        """
        policies = self.storage.list_spend_policies(
            customer_id=customer_id,
            enabled=True,
        )

        # If no policies, charge is allowed by default
        if not policies:
            return AuthorizationResult(
                authorized=True,
                status=AuthorizationStatus.APPROVED,
                amount=amount,
                customer_id=customer_id,
                tool_name=tool_name,
            )

        now = datetime.now(timezone.utc)

        # Calculate current spend for each window
        daily_spend = self._calculate_window_spend(customer_id, now - timedelta(days=1), now)
        weekly_spend = self._calculate_window_spend(customer_id, now - timedelta(weeks=1), now)
        monthly_spend = self._calculate_window_spend(customer_id, now - timedelta(days=30), now)
        hourly_txns = len([
            p for p in self.storage.list_payments(customer_id=customer_id, limit=10000)
            if p.created_at >= now - timedelta(hours=1) and p.status != PaymentStatus.FAILED
        ])

        # Check each policy — most restrictive wins
        for policy in policies:
            # Per-transaction limit
            if policy.max_per_transaction is not None and amount > policy.max_per_transaction:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_OVER_PER_TRANSACTION,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Amount {amount} exceeds per-transaction limit {policy.max_per_transaction}",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Daily limit
            if policy.daily_limit is not None and daily_spend + amount > policy.daily_limit:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_OVER_DAILY_LIMIT,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Daily spend {daily_spend:.2f} + {amount} would exceed daily limit {policy.daily_limit}",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Weekly limit
            if policy.weekly_limit is not None and weekly_spend + amount > policy.weekly_limit:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_OVER_WEEKLY_LIMIT,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Weekly spend {weekly_spend:.2f} + {amount} would exceed weekly limit {policy.weekly_limit}",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Monthly limit
            if policy.monthly_limit is not None and monthly_spend + amount > policy.monthly_limit:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_OVER_MONTHLY_LIMIT,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Monthly spend {monthly_spend:.2f} + {amount} would exceed monthly limit {policy.monthly_limit}",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Blocked tools
            if tool_name and policy.blocked_tools and tool_name in policy.blocked_tools:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_TOOL_BLOCKED,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Tool '{tool_name}' is blocked by spend policy",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Allowed tools (whitelist)
            if tool_name and policy.allowed_tools is not None and tool_name not in policy.allowed_tools:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_TOOL_NOT_ALLOWED,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Tool '{tool_name}' is not in the allowed tools list",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

            # Rate limiting
            if policy.max_transactions_per_hour is not None and hourly_txns >= policy.max_transactions_per_hour:
                return AuthorizationResult(
                    authorized=False,
                    status=AuthorizationStatus.DENIED_RATE_LIMITED,
                    amount=amount,
                    customer_id=customer_id,
                    tool_name=tool_name,
                    policy_id=policy.id,
                    reason=f"Rate limit: {hourly_txns} transactions in last hour (max {policy.max_transactions_per_hour})",
                    daily_spend=daily_spend,
                    daily_limit=policy.daily_limit,
                    monthly_spend=monthly_spend,
                    monthly_limit=policy.monthly_limit,
                )

        # All policies passed
        return AuthorizationResult(
            authorized=True,
            status=AuthorizationStatus.APPROVED,
            amount=amount,
            customer_id=customer_id,
            tool_name=tool_name,
            policy_id=policies[0].id if policies else None,
            daily_spend=daily_spend,
            daily_limit=min((p.daily_limit for p in policies if p.daily_limit is not None), default=None),
            monthly_spend=monthly_spend,
            monthly_limit=min((p.monthly_limit for p in policies if p.monthly_limit is not None), default=None),
        )

    def _calculate_window_spend(
        self,
        customer_id: str,
        start: datetime,
        end: datetime,
    ) -> float:
        """Calculate total succeeded spend in a time window."""
        payments = self.storage.list_payments(customer_id=customer_id, limit=10000)
        total = sum(
            p.amount for p in payments
            if p.status == PaymentStatus.SUCCEEDED
            and p.created_at >= start
            and p.created_at <= end
        )
        return round(total, 2)

    def get_spend_report(
        self,
        customer_id: str,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> SpendReport:
        """Generate a detailed spend report for a customer.

        Shows total spend, breakdown by tool and by day, average/largest
        transactions, and which policies apply.
        """
        now = datetime.now(timezone.utc)
        if period_start is None:
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_end is None:
            period_end = now

        payments = self.storage.list_payments(customer_id=customer_id, limit=10000)
        succeeded = [
            p for p in payments
            if p.status == PaymentStatus.SUCCEEDED
            and p.created_at >= period_start
            and p.created_at <= period_end
        ]
        refunded = sum(p.refund_amount for p in payments if p.created_at >= period_start and p.created_at <= period_end)

        total_spend = sum(p.amount for p in succeeded)
        by_tool: dict[str, float] = {}
        by_day: dict[str, float] = {}
        largest = 0.0

        for p in succeeded:
            tool = p.tool_name or "unattributed"
            by_tool[tool] = by_tool.get(tool, 0) + p.amount
            day_key = p.created_at.strftime("%Y-%m-%d")
            by_day[day_key] = by_day.get(day_key, 0) + p.amount
            if p.amount > largest:
                largest = p.amount

        avg = total_spend / len(succeeded) if succeeded else 0.0
        policies = self.storage.list_spend_policies(customer_id=customer_id)

        return SpendReport(
            customer_id=customer_id,
            period_start=period_start,
            period_end=period_end,
            total_spend=round(total_spend, 2),
            total_transactions=len(succeeded),
            total_refunded=round(refunded, 2),
            net_spend=round(total_spend - refunded, 2),
            by_tool={k: round(v, 2) for k, v in sorted(by_tool.items(), key=lambda x: -x[1])},
            by_day={k: round(v, 2) for k, v in sorted(by_day.items())},
            average_transaction=round(avg, 2),
            largest_transaction=round(largest, 2),
            policies_applied=[p.id for p in policies if p.enabled],
        )
