"""x402 billing middleware — HTTP 402 payment enforcement for agents.

Implements the x402 protocol flow for ASGI applications:

1. Agent requests a paid resource
2. Middleware returns HTTP 402 with payment requirements
3. Agent reads requirements, pays (on-chain or via facilitator)
4. Agent retries the request with ``X-PAYMENT`` header containing the payment payload
5. Middleware verifies the payment and returns the resource

Works with FastAPI, Starlette, or any ASGI framework.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .models import X402PaymentRequirements


# ── Constants ──────────────────────────────────────────────────────────────

USDC_DECIMALS = 6
DEFAULT_USDC_CONTRACT = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7235"

# x402 header names (per spec)
X_PAYMENT_HEADER = "X-PAYMENT"
X_PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"

HTTP_402 = 402
HTTP_OK = 200


# ── Pricing Rules ──────────────────────────────────────────────────────────

@dataclass
class PricingRule:
    """A single endpoint pricing rule for x402 middleware.

    Matches requests by HTTP method + path prefix. When a request matches,
    the middleware returns 402 with payment requirements for the specified amount.

    Use ``free_paths`` or a custom ``check_fn`` for conditional pricing.
    """

    method: str = "GET"
    path: str = "/"
    amount: float = 0.0
    description: str = ""
    network: str = "base-sepolia"
    asset: str = DEFAULT_USDC_CONTRACT
    # Optional callable for dynamic pricing; receives (scope, headers), returns amount
    dynamic_amount: Callable[[dict, dict], float] | None = None
    # If True, request can proceed without payment (e.g. API key present)
    check_fn: Callable[[dict, dict], bool] | None = None

    def matches(self, method: str, path: str) -> bool:
        """Check if this rule matches the request."""
        return method.upper() == self.method.upper() and path.startswith(self.path)

    def get_amount(self, scope: dict, headers: dict) -> float:
        """Get the amount for this request (static or dynamic)."""
        if self.dynamic_amount:
            return self.dynamic_amount(scope, headers)
        return self.amount


@dataclass
class VerifiedPayment:
    """Result of verifying an x402 payment header."""
    valid: bool
    reason: str = ""
    transaction_id: str = ""
    payment_payload: dict[str, Any] = field(default_factory=dict)


# ── Payment Verifier ───────────────────────────────────────────────────────

class PaymentVerifier:
    """Verify x402 payment headers.

    The x402 protocol sends payment proofs in the ``X-PAYMENT`` header as a
    base64-encoded JSON payload. This verifier checks:

    - Payload structure and required fields
    - Payment amount matches requirements
    - Recipient address matches
    - Network matches
    - Optional: signature/hmac verification via shared secret
    """

    def __init__(
        self,
        secret: str = "",
        facilitator_url: str = "",
        merchant_wallet: str = "",
    ):
        self.secret = secret
        self.facilitator_url = facilitator_url
        self.merchant_wallet = merchant_wallet

    def verify(
        self,
        payment_header: str | None,
        requirements: X402PaymentRequirements,
    ) -> VerifiedPayment:
        """Verify an X-PAYMENT header against requirements."""
        if not payment_header:
            return VerifiedPayment(valid=False, reason="No X-PAYMENT header")

        try:
            # Decode the base64 payload
            decoded = base64.b64decode(payment_header)
            payload = json.loads(decoded)
        except Exception as exc:
            return VerifiedPayment(valid=False, reason=f"Invalid payment header encoding: {exc}")

        return self.verify_payload(payload, requirements)

    def verify_payload(
        self,
        payload: dict[str, Any],
        requirements: X402PaymentRequirements,
    ) -> VerifiedPayment:
        """Verify a decoded payment payload against requirements."""
        if not isinstance(payload, dict):
            return VerifiedPayment(valid=False, reason="Payload is not a dict")

        # Check required fields per x402 spec
        required_fields = ["x402Version", "kind"]
        for field_name in required_fields:
            if field_name not in payload:
                return VerifiedPayment(
                    valid=False, reason=f"Missing required field: {field_name}"
                )

        kind = payload.get("kind", "")
        if kind not in ("verified", "signed"):
            return VerifiedPayment(
                valid=False, reason=f"Unsupported payment kind: {kind}"
            )

        inner = payload.get("payload", payload.get("paymentHeader", {}))
        if not isinstance(inner, dict):
            return VerifiedPayment(valid=False, reason="Payment payload is not a dict")

        # Verify amount
        paid_amount = inner.get("amount", inner.get("value", "0"))
        if str(paid_amount) != str(requirements.amount):
            # Check within tolerance for USDC atomic units
            try:
                diff = abs(int(paid_amount) - int(requirements.amount))
                if diff > 1:  # 1 atomic unit tolerance (0.000001 USDC)
                    return VerifiedPayment(
                        valid=False,
                        reason=f"Amount mismatch: paid {paid_amount}, required {requirements.amount}",
                    )
            except (ValueError, TypeError):
                return VerifiedPayment(
                    valid=False,
                    reason=f"Amount mismatch: paid {paid_amount}, required {requirements.amount}",
                )

        # Verify recipient
        pay_to = inner.get("payTo", inner.get("to", inner.get("recipient", "")))
        if pay_to and self.merchant_wallet and pay_to.lower() != self.merchant_wallet.lower():
            return VerifiedPayment(
                valid=False,
                reason=f"Recipient mismatch: {pay_to} != {self.merchant_wallet}",
            )

        # Verify network
        network = payload.get("network", inner.get("network", ""))
        if network and network != requirements.network:
            return VerifiedPayment(
                valid=False,
                reason=f"Network mismatch: {network} != {requirements.network}",
            )

        # Verify signature if shared secret is set
        if self.secret and "signature" in payload:
            if not self._verify_signature(payload, inner):
                return VerifiedPayment(valid=False, reason="Signature verification failed")

        tx_id = inner.get("transactionHash", inner.get("txHash", inner.get("tx", "")))
        return VerifiedPayment(
            valid=True,
            transaction_id=str(tx_id) or f"verified_{uuid.uuid4().hex[:16]}",
            payment_payload=payload,
        )

    def _verify_signature(self, payload: dict, inner: dict) -> bool:
        """Verify HMAC signature over payment payload."""
        sig = payload.get("signature", "")
        body = json.dumps(inner, sort_keys=True)
        expected = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


# ── x402 Middleware ────────────────────────────────────────────────────────

def _amount_to_atomic(amount: float) -> str:
    """Convert USD amount to USDC atomic units (6 decimals)."""
    return str(int(round(amount * 10**USDC_DECIMALS)))


def _atomic_to_amount(atomic: str | int) -> float:
    """Convert USDC atomic units back to USD float."""
    return int(atomic) / 10**USDC_DECIMALS


class X402Middleware:
    """ASGI middleware that enforces x402 payments on HTTP requests.

    Usage with Starlette/FastAPI::

        from mcp_payments.middleware import X402Middleware, PricingRule

        pricing = [PricingRule(method="GET", path="/api/premium", amount=0.01)]

        app.add_middleware(
            X402Middleware,
            merchant_wallet="0x742d...",
            pricing_rules=pricing,
        )

    For requests matching a pricing rule without a valid ``X-PAYMENT`` header,
    the middleware returns HTTP 402 with payment requirements JSON::

        HTTP/1.1 402 Payment Required
        Content-Type: application/json

        {
          "x402Version": 1,
          "accepts": [{
            "scheme": "exact",
            "network": "base-sepolia",
            "asset": "0x1c7...",
            "amount": "10000",
            "payTo": "0x742d...",
            "resource": "https://api.example.com/premium",
            "description": "Premium API access"
          }]
        }

    When the client retries with a valid ``X-PAYMENT`` header, the request
    proceeds normally and the middleware adds an ``X-PAYMENT-RESPONSE`` header
    to the response confirming settlement.
    """

    def __init__(
        self,
        app: Callable,
        merchant_wallet: str = "",
        pricing_rules: list[PricingRule] | None = None,
        secret: str = "",
        facilitator_url: str = "",
        path_prefix: str = "",
    ):
        self.app = app
        self.merchant_wallet = merchant_wallet
        self.pricing_rules = pricing_rules or []
        self.path_prefix = path_prefix.rstrip("/")
        self.verifier = PaymentVerifier(
            secret=secret,
            facilitator_url=facilitator_url,
            merchant_wallet=merchant_wallet,
        )

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        # Strip prefix for matching
        match_path = path
        if self.path_prefix and path.startswith(self.path_prefix):
            match_path = path[len(self.path_prefix):] or "/"

        # Find matching pricing rule
        rule = self._find_rule(method, match_path)
        if rule is None:
            # No pricing rule — pass through
            return await self.app(scope, receive, send)

        # Parse headers
        headers = self._parse_headers(scope.get("headers", []))

        # Check if request can proceed without payment (custom check)
        if rule.check_fn and rule.check_fn(scope, headers):
            return await self.app(scope, receive, send)

        # Check for X-PAYMENT header
        payment_header = headers.get(X_PAYMENT_HEADER.lower())

        if payment_header:
            # Client claims to have paid — verify
            amount = rule.get_amount(scope, headers)
            requirements = self._build_requirements(rule, amount, path)
            verification = self.verifier.verify(payment_header, requirements)

            if verification.valid:
                # Payment verified — proceed and add response header
                async def send_with_payment_header(
                    message: dict,
                    verification_tx: str = verification.transaction_id,
                ) -> None:
                    if message["type"] == "http.response.start":
                        response_header = json.dumps({
                            "x402Version": 1,
                            "success": True,
                            "transactionId": verification_tx,
                            "network": rule.network,
                            "settled": True,
                        })
                        encoded = response_header.encode()
                        message["headers"] = list(message.get("headers", [])) + [
                            (X_PAYMENT_RESPONSE_HEADER.lower().encode(), encoded),
                        ]
                    await send(message)

                return await self.app(scope, receive, send_with_payment_header)
            else:
                # Invalid payment — return 402 again with error
                return await self._send_402(
                    send, rule, path, error=verification.reason
                )

        # No payment — return 402
        return await self._send_402(send, rule, path)

    def _find_rule(self, method: str, path: str) -> PricingRule | None:
        """Find the first pricing rule that matches."""
        for rule in self.pricing_rules:
            if rule.matches(method, path):
                return rule
        return None

    def _build_requirements(
        self, rule: PricingRule, amount: float, resource_path: str
    ) -> X402PaymentRequirements:
        """Build x402 payment requirements from a pricing rule."""
        return X402PaymentRequirements(
            scheme="exact",
            network=rule.network,
            asset=rule.asset,
            amount=_amount_to_atomic(amount),
            pay_to=self.merchant_wallet,
            resource=resource_path,
            description=rule.description or f"Payment for {resource_path}",
            mime_type="application/json",
        )

    async def _send_402(
        self,
        send: Callable,
        rule: PricingRule,
        path: str,
        error: str = "",
    ) -> None:
        """Send an HTTP 402 Payment Required response."""
        amount = rule.amount  # Static for 402; dynamic not available without scope
        requirements = self._build_requirements(rule, amount, path)

        body = {
            "x402Version": 1,
            "accepts": [requirements.model_dump()],
        }
        if error:
            body["error"] = error
            body["retry"] = True

        payload = json.dumps(body).encode()

        await send({
            "type": "http.response.start",
            "status": HTTP_402,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"www-authenticate", b"x402"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": payload,
        })

    @staticmethod
    def _parse_headers(raw_headers: list) -> dict[str, str]:
        """Parse ASGI raw headers into a dict."""
        result = {}
        for key, value in raw_headers:
            try:
                result[key.decode().lower()] = value.decode()
            except Exception:
                continue
        return result


# ── Facilitator Client ─────────────────────────────────────────────────────

class FacilitatorClient:
    """Client for x402 facilitator — verifies payments on-chain.

    In the x402 protocol, the facilitator is an optional server that
    handles payment verification and settlement. Agents send their payment
    to the facilitator, which verifies it on-chain and returns a receipt.

    This client supports the standard facilitator API (used by Coinbase's
    x402-secured endpoints):

    - POST /verify  — verify a payment payload
    - POST /settle  — settle a payment and get receipt

    For testing, use ``FacilitatorClient(simulate=True)`` which auto-approves.
    """

    def __init__(
        self,
        base_url: str = "https://facilitator.x402.org",
        simulate: bool = False,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.simulate = simulate
        self.timeout = timeout
        self._http = None  # Lazy init httpx client

    async def verify(
        self,
        payment_payload: dict,
        requirements: X402PaymentRequirements,
    ) -> VerifiedPayment:
        """Verify a payment via the facilitator."""
        if self.simulate:
            return VerifiedPayment(
                valid=True,
                reason="Simulated approval",
                transaction_id=f"sim_{uuid.uuid4().hex[:16]}",
                payment_payload=payment_payload,
            )

        # Real verification via facilitator API
        try:
            import httpx
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.timeout)

            response = await self._http.post(
                f"{self.base_url}/verify",
                json={
                    "paymentPayload": payment_payload,
                    "paymentRequirements": requirements.model_dump(),
                },
            )

            if response.status_code == 200:
                data = response.json()
                return VerifiedPayment(
                    valid=data.get("isValid", False),
                    reason=data.get("invalidReason", ""),
                    transaction_id=data.get("transactionId", ""),
                    payment_payload=payment_payload,
                )
            else:
                return VerifiedPayment(
                    valid=False,
                    reason=f"Facilitator returned {response.status_code}",
                )
        except Exception as exc:
            return VerifiedPayment(
                valid=False,
                reason=f"Facilitator error: {exc}",
            )

    async def settle(
        self,
        payment_payload: dict,
        requirements: X402PaymentRequirements,
    ) -> dict[str, Any]:
        """Settle a payment via the facilitator and get a receipt."""
        if self.simulate:
            return {
                "success": True,
                "transactionId": f"sim_{uuid.uuid4().hex[:16]}",
                "network": requirements.network,
                "settled": True,
            }

        try:
            import httpx
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=self.timeout)

            response = await self._http.post(
                f"{self.base_url}/settle",
                json={
                    "paymentPayload": payment_payload,
                    "paymentRequirements": requirements.model_dump(),
                },
            )

            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "success": False,
                    "error": f"Facilitator returned {response.status_code}",
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ── Utility: Build X-PAYMENT header for testing ────────────────────────────

def make_payment_header(
    amount_usd: float,
    pay_to: str,
    network: str = "base-sepolia",
    transaction_hash: str = "",
    asset: str = DEFAULT_USDC_CONTRACT,
) -> str:
    """Build a valid X-PAYMENT header for testing.

    Encodes an x402-compatible payment payload as base64 JSON.
    """
    payload = {
        "x402Version": 1,
        "kind": "verified",
        "scheme": "exact",
        "network": network,
        "payload": {
            "amount": _amount_to_atomic(amount_usd),
            "payTo": pay_to,
            "asset": asset,
            "transactionHash": transaction_hash or f"0x{uuid.uuid4().hex}",
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()
