"""In-memory storage with JSON persistence. Production-ready swap to SQLModel."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    Customer,
    Payment,
    PaymentIntent,
    PaymentStatus,
    Refund,
    ToolPricing,
)


class Storage:
    """Thread-safe JSON-file backed storage."""

    def __init__(self, data_dir: str | Path | None = None):
        self._data_dir = Path(data_dir) if data_dir else Path.home() / ".mcp-payments"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "payments.json"
        self._lock = threading.RLock()
        self._customers: dict[str, Customer] = {}
        self._payments: dict[str, Payment] = {}
        self._intents: dict[str, PaymentIntent] = {}
        self._refunds: dict[str, Refund] = {}
        self._tool_pricing: dict[str, ToolPricing] = {}
        self._load()

    # ── Persistence ────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._db_path.exists():
            return
        try:
            raw = json.loads(self._db_path.read_text())
            self._customers = {c["id"]: Customer(**c) for c in raw.get("customers", [])}
            self._payments = {p["id"]: Payment(**p) for p in raw.get("payments", [])}
            self._intents = {i["id"]: PaymentIntent(**i) for i in raw.get("intents", [])}
            self._refunds = {r["id"]: Refund(**r) for r in raw.get("refunds", [])}
            self._tool_pricing = {t["tool_name"]: ToolPricing(**t) for t in raw.get("tool_pricing", [])}
        except Exception:
            pass  # Corrupt DB — start fresh

    def _save(self) -> None:
        data = {
            "customers": [c.model_dump(mode="json") for c in self._customers.values()],
            "payments": [p.model_dump(mode="json") for p in self._payments.values()],
            "intents": [i.model_dump(mode="json") for i in self._intents.values()],
            "refunds": [r.model_dump(mode="json") for r in self._refunds.values()],
            "tool_pricing": [t.model_dump(mode="json") for t in self._tool_pricing.values()],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._db_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(self._db_path)

    # ── Customers ──────────────────────────────────────────────────────

    def create_customer(self, customer: Customer) -> Customer:
        with self._lock:
            self._customers[customer.id] = customer
            self._save()
            return customer

    def get_customer(self, customer_id: str) -> Optional[Customer]:
        with self._lock:
            return self._customers.get(customer_id)

    def list_customers(self, limit: int = 100) -> list[Customer]:
        with self._lock:
            return list(self._customers.values())[:limit]

    def update_customer_balance(self, customer_id: str, delta: float) -> Optional[Customer]:
        with self._lock:
            c = self._customers.get(customer_id)
            if c is None:
                return None
            c.balance += delta
            self._save()
            return c

    # ── Payments ───────────────────────────────────────────────────────

    def create_payment(self, payment: Payment) -> Payment:
        with self._lock:
            self._payments[payment.id] = payment
            self._save()
            return payment

    def get_payment(self, payment_id: str) -> Optional[Payment]:
        with self._lock:
            return self._payments.get(payment_id)

    def update_payment(self, payment_id: str, **kwargs) -> Optional[Payment]:
        with self._lock:
            p = self._payments.get(payment_id)
            if p is None:
                return None
            for k, v in kwargs.items():
                if hasattr(p, k) and v is not None:
                    setattr(p, k, v)
            p.updated_at = datetime.now(timezone.utc)
            self._save()
            return p

    def list_payments(
        self,
        customer_id: Optional[str] = None,
        status: Optional[PaymentStatus] = None,
        limit: int = 100,
    ) -> list[Payment]:
        with self._lock:
            results = list(self._payments.values())
            if customer_id:
                results = [p for p in results if p.customer_id == customer_id]
            if status:
                results = [p for p in results if p.status == status]
            return results[:limit]

    # ── Payment Intents ────────────────────────────────────────────────

    def create_intent(self, intent: PaymentIntent) -> PaymentIntent:
        with self._lock:
            self._intents[intent.id] = intent
            self._save()
            return intent

    def get_intent(self, intent_id: str) -> Optional[PaymentIntent]:
        with self._lock:
            return self._intents.get(intent_id)

    def update_intent(self, intent_id: str, **kwargs) -> Optional[PaymentIntent]:
        with self._lock:
            i = self._intents.get(intent_id)
            if i is None:
                return None
            for k, v in kwargs.items():
                if hasattr(i, k) and v is not None:
                    setattr(i, k, v)
            self._save()
            return i

    # ── Refunds ────────────────────────────────────────────────────────

    def create_refund(self, refund: Refund) -> Refund:
        with self._lock:
            self._refunds[refund.id] = refund
            self._save()
            return refund

    def get_refund(self, refund_id: str) -> Optional[Refund]:
        with self._lock:
            return self._refunds.get(refund_id)

    def list_refunds(self, payment_id: Optional[str] = None) -> list[Refund]:
        with self._lock:
            results = list(self._refunds.values())
            if payment_id:
                results = [r for r in results if r.payment_id == payment_id]
            return results

    # ── Tool Pricing ───────────────────────────────────────────────────

    def set_tool_pricing(self, pricing: ToolPricing) -> ToolPricing:
        with self._lock:
            self._tool_pricing[pricing.tool_name] = pricing
            self._save()
            return pricing

    def get_tool_pricing(self, tool_name: str) -> Optional[ToolPricing]:
        with self._lock:
            return self._tool_pricing.get(tool_name)

    def list_tool_pricing(self) -> list[ToolPricing]:
        with self._lock:
            return list(self._tool_pricing.values())

    def remove_tool_pricing(self, tool_name: str) -> bool:
        with self._lock:
            if tool_name in self._tool_pricing:
                del self._tool_pricing[tool_name]
                self._save()
                return True
            return False
