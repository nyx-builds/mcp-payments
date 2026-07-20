"""Payment processing engine — core business logic."""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import (
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
    SplitPayment,
    SplitShare,
    SplitStatus,
    ToolPricing,
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
