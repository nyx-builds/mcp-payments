"""In-memory storage with JSON persistence. Production-ready swap to SQLModel."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    Customer,
    Escrow,
    Payment,
    PaymentIntent,
    PaymentStatus,
    Refund,
    ServiceListing,
    ServiceReview,
    SplitPayment,
    SpendPolicy,
    SubscriptionPlan,
    ToolPricing,
    UsageEvent,
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
        self._escrows: dict[str, Escrow] = {}
        self._splits: dict[str, SplitPayment] = {}
        self._usage_events: dict[str, "UsageEvent"] = {}
        self._services: dict[str, "ServiceListing"] = {}
        self._plans: dict[str, "SubscriptionPlan"] = {}
        self._reviews: dict[str, "ServiceReview"] = {}
        self._spend_policies: dict[str, "SpendPolicy"] = {}
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
            self._escrows = {e["id"]: Escrow(**e) for e in raw.get("escrows", [])}
            self._splits = {s["id"]: SplitPayment(**s) for s in raw.get("splits", [])}
            self._usage_events = {u["id"]: UsageEvent(**u) for u in raw.get("usage_events", [])}
            self._services = {s["id"]: ServiceListing(**s) for s in raw.get("services", [])}
            self._plans = {p["id"]: SubscriptionPlan(**p) for p in raw.get("plans", [])}
            self._reviews = {r["id"]: ServiceReview(**r) for r in raw.get("reviews", [])}
            self._spend_policies = {p["id"]: SpendPolicy(**p) for p in raw.get("spend_policies", [])}
        except Exception:
            pass  # Corrupt DB — start fresh

    def _save(self) -> None:
        data = {
            "customers": [c.model_dump(mode="json") for c in self._customers.values()],
            "payments": [p.model_dump(mode="json") for p in self._payments.values()],
            "intents": [i.model_dump(mode="json") for i in self._intents.values()],
            "refunds": [r.model_dump(mode="json") for r in self._refunds.values()],
            "tool_pricing": [t.model_dump(mode="json") for t in self._tool_pricing.values()],
            "escrows": [e.model_dump(mode="json") for e in self._escrows.values()],
            "splits": [s.model_dump(mode="json") for s in self._splits.values()],
            "usage_events": [u.model_dump(mode="json") for u in self._usage_events.values()],
            "services": [s.model_dump(mode="json") for s in self._services.values()],
            "plans": [p.model_dump(mode="json") for p in self._plans.values()],
            "reviews": [r.model_dump(mode="json") for r in self._reviews.values()],
            "spend_policies": [p.model_dump(mode="json") for p in self._spend_policies.values()],
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

    # ── Escrow (v0.2.0) ────────────────────────────────────────────────

    def create_escrow(self, escrow: Escrow) -> Escrow:
        with self._lock:
            self._escrows[escrow.id] = escrow
            self._save()
            return escrow

    def get_escrow(self, escrow_id: str) -> Optional[Escrow]:
        with self._lock:
            return self._escrows.get(escrow_id)

    def update_escrow(self, escrow_id: str, **kwargs) -> Optional[Escrow]:
        with self._lock:
            e = self._escrows.get(escrow_id)
            if e is None:
                return None
            for k, v in kwargs.items():
                if hasattr(e, k) and v is not None:
                    setattr(e, k, v)
            self._save()
            return e

    def list_escrows(
        self,
        payer_id: Optional[str] = None,
        payee_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[Escrow]:
        with self._lock:
            results = list(self._escrows.values())
            if payer_id:
                results = [e for e in results if e.payer_customer_id == payer_id]
            if payee_id:
                results = [e for e in results if e.payee_customer_id == payee_id]
            if status:
                results = [e for e in results if e.status.value == status]
            return results[:limit]

    # ── Split Payments (v0.2.0) ────────────────────────────────────────

    def create_split(self, split: SplitPayment) -> SplitPayment:
        with self._lock:
            self._splits[split.id] = split
            self._save()
            return split

    def get_split(self, split_id: str) -> Optional[SplitPayment]:
        with self._lock:
            return self._splits.get(split_id)

    def update_split(self, split_id: str, **kwargs) -> Optional[SplitPayment]:
        with self._lock:
            s = self._splits.get(split_id)
            if s is None:
                return None
            for k, v in kwargs.items():
                if hasattr(s, k) and v is not None:
                    setattr(s, k, v)
            self._save()
            return s

    def list_splits(
        self,
        payer_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[SplitPayment]:
        with self._lock:
            results = list(self._splits.values())
            if payer_id:
                results = [s for s in results if s.payer_customer_id == payer_id]
            if status:
                results = [s for s in results if s.status.value == status]
            return results[:limit]

    # ── Usage Events (v0.4.0 — Metering) ──────────────────────────────

    def create_usage_event(self, event: "UsageEvent") -> "UsageEvent":
        with self._lock:
            self._usage_events[event.id] = event
            self._save()
            return event

    def get_usage_event(self, event_id: str) -> Optional["UsageEvent"]:
        with self._lock:
            return self._usage_events.get(event_id)

    def list_usage_events(
        self,
        customer_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        settled: Optional[bool] = None,
        since: Optional["datetime"] = None,
        until: Optional["datetime"] = None,
        limit: int = 10000,
    ) -> list["UsageEvent"]:
        from datetime import datetime as dt
        with self._lock:
            results = list(self._usage_events.values())
            if customer_id:
                results = [e for e in results if e.customer_id == customer_id]
            if tool_name:
                results = [e for e in results if e.tool_name == tool_name]
            if settled is not None:
                results = [e for e in results if e.settled == settled]
            if since:
                results = [e for e in results if e.timestamp >= since]
            if until:
                results = [e for e in results if e.timestamp <= until]
            results.sort(key=lambda e: e.timestamp)
            return results[:limit]

    def mark_events_settled(self, event_ids: list[str]) -> int:
        """Mark a batch of usage events as settled. Returns count updated."""
        with self._lock:
            count = 0
            for eid in event_ids:
                ev = self._usage_events.get(eid)
                if ev and not ev.settled:
                    ev.settled = True
                    count += 1
            if count:
                self._save()
            return count

    def delete_usage_event(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._usage_events:
                del self._usage_events[event_id]
                self._save()
                return True
            return False

    # ── Marketplace: Service Listings (v0.5.0) ─────────────────────────

    def create_service(self, service: "ServiceListing") -> "ServiceListing":
        with self._lock:
            self._services[service.id] = service
            self._save()
            return service

    def get_service(self, service_id: str) -> Optional["ServiceListing"]:
        with self._lock:
            return self._services.get(service_id)

    def get_service_by_slug(self, slug: str) -> Optional["ServiceListing"]:
        with self._lock:
            for s in self._services.values():
                if s.slug == slug:
                    return s
            return None

    def update_service(self, service_id: str, **kwargs) -> Optional["ServiceListing"]:
        with self._lock:
            s = self._services.get(service_id)
            if s is None:
                return None
            for k, v in kwargs.items():
                if hasattr(s, k) and v is not None:
                    setattr(s, k, v)
            s.updated_at = datetime.now(timezone.utc)
            self._save()
            return s

    def list_services(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        provider_id: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> list["ServiceListing"]:
        with self._lock:
            results = list(self._services.values())
            if status:
                results = [s for s in results if s.status.value == status]
            if category:
                results = [s for s in results if s.category == category]
            if provider_id:
                results = [s for s in results if s.provider_customer_id == provider_id]
            if tag:
                results = [s for s in results if tag in s.tags]
            return results[:limit]

    def search_services(self, query: str, limit: int = 20) -> list["ServiceListing"]:
        """Full-text search across name, description, tags, and category."""
        query_lower = query.lower()
        if not query_lower.strip():
            return []
        with self._lock:
            scored: list[tuple[float, "ServiceListing"]] = []
            for s in self._services.values():
                if s.status.value not in ("active", "deprecated"):
                    continue
                score = 0.0
                if query_lower in s.name.lower():
                    score += 3.0
                if query_lower in s.description.lower():
                    score += 1.0
                if query_lower in s.category.lower():
                    score += 2.0
                for t in s.tags:
                    if query_lower in t.lower():
                        score += 1.5
                if score > 0:
                    scored.append((score, s))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [s for _, s in scored[:limit]]

    def delete_service(self, service_id: str) -> bool:
        with self._lock:
            if service_id in self._services:
                del self._services[service_id]
                self._save()
                return True
            return False

    # ── Marketplace: Subscription Plans ────────────────────────────────

    def create_plan(self, plan: "SubscriptionPlan") -> "SubscriptionPlan":
        with self._lock:
            self._plans[plan.id] = plan
            self._save()
            return plan

    def get_plan(self, plan_id: str) -> Optional["SubscriptionPlan"]:
        with self._lock:
            return self._plans.get(plan_id)

    def list_plans(self, service_id: Optional[str] = None) -> list["SubscriptionPlan"]:
        with self._lock:
            results = list(self._plans.values())
            if service_id:
                results = [p for p in results if p.service_id == service_id]
            return results

    def delete_plan(self, plan_id: str) -> bool:
        with self._lock:
            if plan_id in self._plans:
                del self._plans[plan_id]
                self._save()
                return True
            return False

    # ── Marketplace: Reviews ───────────────────────────────────────────

    def create_review(self, review: "ServiceReview") -> "ServiceReview":
        with self._lock:
            self._reviews[review.id] = review
            self._save()
            return review

    def get_review(self, review_id: str) -> Optional["ServiceReview"]:
        with self._lock:
            return self._reviews.get(review_id)

    def list_reviews(
        self,
        service_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        limit: int = 100,
    ) -> list["ServiceReview"]:
        with self._lock:
            results = list(self._reviews.values())
            if service_id:
                results = [r for r in results if r.service_id == service_id]
            if customer_id:
                results = [r for r in results if r.customer_id == customer_id]
            return results[:limit]

    def delete_review(self, review_id: str) -> bool:
        with self._lock:
            if review_id in self._reviews:
                del self._reviews[review_id]
                self._save()
                return True
            return False

    # ── Spend Policies (v0.6.0 — Agent Spend Controls) ────────────────

    def create_spend_policy(self, policy: "SpendPolicy") -> "SpendPolicy":
        with self._lock:
            self._spend_policies[policy.id] = policy
            self._save()
            return policy

    def get_spend_policy(self, policy_id: str) -> Optional["SpendPolicy"]:
        with self._lock:
            return self._spend_policies.get(policy_id)

    def update_spend_policy(self, policy_id: str, **kwargs) -> Optional["SpendPolicy"]:
        with self._lock:
            p = self._spend_policies.get(policy_id)
            if p is None:
                return None
            for k, v in kwargs.items():
                if hasattr(p, k) and v is not None:
                    setattr(p, k, v)
            p.updated_at = datetime.now(timezone.utc)
            self._save()
            return p

    def delete_spend_policy(self, policy_id: str) -> bool:
        with self._lock:
            if policy_id in self._spend_policies:
                del self._spend_policies[policy_id]
                self._save()
                return True
            return False

    def list_spend_policies(
        self,
        customer_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> list["SpendPolicy"]:
        with self._lock:
            results = list(self._spend_policies.values())
            if customer_id:
                results = [p for p in results if p.customer_id == customer_id]
            if enabled is not None:
                results = [p for p in results if p.enabled == enabled]
            return results
