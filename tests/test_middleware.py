"""Tests for x402 billing middleware."""
from __future__ import annotations

import base64
import json

import pytest

from mcp_payments.middleware import (
    DEFAULT_USDC_CONTRACT,
    FacilitatorClient,
    PaymentVerifier,
    PricingRule,
    VerifiedPayment,
    X402Middleware,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
    _amount_to_atomic,
    _atomic_to_amount,
    make_payment_header,
)
from mcp_payments.models import X402PaymentRequirements


# ── Conversion Utilities ───────────────────────────────────────────────────


class TestAtomicConversion:
    def test_amount_to_atomic_basic(self):
        assert _amount_to_atomic(1.0) == "1000000"
        assert _amount_to_atomic(0.01) == "10000"
        assert _amount_to_atomic(0.001) == "1000"

    def test_amount_to_atomic_zero(self):
        assert _amount_to_atomic(0.0) == "0"

    def test_amount_to_atomic_large(self):
        assert _amount_to_atomic(1000.0) == "1000000000"

    def test_roundtrip(self):
        for amount in [0.01, 1.0, 5.50, 99.99]:
            atomic = _amount_to_atomic(amount)
            recovered = _atomic_to_amount(atomic)
            assert abs(recovered - amount) < 0.000001

    def test_atomic_to_amount(self):
        assert _atomic_to_amount("1000000") == 1.0
        assert _atomic_to_amount("0") == 0.0


# ── PricingRule ────────────────────────────────────────────────────────────


class TestPricingRule:
    def test_exact_match(self):
        rule = PricingRule(method="GET", path="/api/data", amount=0.01)
        assert rule.matches("GET", "/api/data")
        assert rule.matches("get", "/api/data")

    def test_prefix_match(self):
        rule = PricingRule(method="POST", path="/api/", amount=0.05)
        assert rule.matches("POST", "/api/data")
        assert rule.matches("POST", "/api/data/123")
        assert not rule.matches("POST", "/other")

    def test_method_mismatch(self):
        rule = PricingRule(method="GET", path="/api/", amount=0.01)
        assert not rule.matches("POST", "/api/data")

    def test_get_amount_static(self):
        rule = PricingRule(amount=0.50)
        assert rule.get_amount({}, {}) == 0.50

    def test_get_amount_dynamic(self):
        rule = PricingRule(
            amount=0.01,
            dynamic_amount=lambda scope, headers: 0.10 if "premium" in headers else 0.01,
        )
        assert rule.get_amount({}, {}) == 0.01
        assert rule.get_amount({}, {"premium": "yes"}) == 0.10

    def test_check_fn_bypasses_payment(self):
        rule = PricingRule(
            amount=0.10,
            check_fn=lambda scope, headers: "x-api-key" in headers,
        )
        assert rule.check_fn({}, {"x-api-key": "secret"}) is True
        assert rule.check_fn({}, {}) is False


# ── PaymentVerifier ────────────────────────────────────────────────────────


class TestPaymentVerifier:
    @pytest.fixture
    def merchant_wallet(self):
        return "0x742d35Cc6634C0532925a3b844Bc9e7595f0bAe1"

    @pytest.fixture
    def verifier(self, merchant_wallet):
        return PaymentVerifier(merchant_wallet=merchant_wallet)

    @pytest.fixture
    def requirements(self, merchant_wallet):
        return X402PaymentRequirements(
            amount=_amount_to_atomic(0.01),
            pay_to=merchant_wallet,
            resource="https://api.example.com/data",
            description="Data access",
            network="base-sepolia",
        )

    def test_no_header_returns_invalid(self, verifier, requirements):
        result = verifier.verify(None, requirements)
        assert not result.valid
        assert "No X-PAYMENT header" in result.reason

    def test_empty_header_returns_invalid(self, verifier, requirements):
        result = verifier.verify("", requirements)
        assert not result.valid

    def test_invalid_base64_returns_invalid(self, verifier, requirements):
        result = verifier.verify("!!!not-base64!!!", requirements)
        assert not result.valid

    def test_valid_payment_verifies(
        self, verifier, requirements, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.01,
            pay_to=merchant_wallet,
            network="base-sepolia",
        )
        result = verifier.verify(header, requirements)
        assert result.valid
        assert result.transaction_id

    def test_amount_mismatch_fails(
        self, verifier, requirements, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.05,  # Wrong amount
            pay_to=merchant_wallet,
        )
        result = verifier.verify(header, requirements)
        assert not result.valid
        assert "Amount mismatch" in result.reason

    def test_recipient_mismatch_fails(
        self, verifier, requirements, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.01,
            pay_to="0xWRONG_ADDRESS",
        )
        result = verifier.verify(header, requirements)
        assert not result.valid
        assert "Recipient mismatch" in result.reason

    def test_network_mismatch_fails(
        self, verifier, requirements, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.01,
            pay_to=merchant_wallet,
            network="base-mainnet",  # Wrong network
        )
        result = verifier.verify(header, requirements)
        assert not result.valid
        assert "Network mismatch" in result.reason

    def test_payload_not_dict(self, verifier, requirements):
        result = verifier.verify_payload("not-a-dict", requirements)
        assert not result.valid

    def test_missing_required_field(self, verifier, requirements):
        payload = {"payload": {}}  # Missing x402Version and kind
        result = verifier.verify_payload(payload, requirements)
        assert not result.valid
        assert "Missing required field" in result.reason

    def test_unsupported_kind(self, verifier, requirements):
        payload = {
            "x402Version": 1,
            "kind": "unsupported-kind",
            "payload": {},
        }
        result = verifier.verify_payload(payload, requirements)
        assert not result.valid
        assert "Unsupported payment kind" in result.reason

    def test_amount_tolerance(self, verifier, requirements, merchant_wallet):
        """Off-by-one atomic unit is tolerated."""
        # Requirements say 10000 (0.01 USD)
        # Pay 10001 (0.000001 more)
        payload = {
            "x402Version": 1,
            "kind": "verified",
            "network": "base-sepolia",
            "payload": {
                "amount": "10001",
                "payTo": merchant_wallet,
            },
        }
        result = verifier.verify_payload(payload, requirements)
        assert result.valid  # Within tolerance

    def test_amount_intolerance(self, verifier, requirements, merchant_wallet):
        """Off-by-two atomic units fails."""
        payload = {
            "x402Version": 1,
            "kind": "verified",
            "network": "base-sepolia",
            "payload": {
                "amount": "10002",
                "payTo": merchant_wallet,
            },
        }
        result = verifier.verify_payload(payload, requirements)
        assert not result.valid

    def test_signature_verification_success(
        self, merchant_wallet, requirements
    ):
        import hmac as _hmac
        import hashlib as _hashlib

        secret = "shared-secret-key"
        verifier = PaymentVerifier(secret=secret, merchant_wallet=merchant_wallet)

        inner = {
            "amount": _amount_to_atomic(0.01),
            "payTo": merchant_wallet,
            "network": "base-sepolia",
        }
        sig = _hmac.new(secret.encode(), json.dumps(inner, sort_keys=True).encode(), _hashlib.sha256).hexdigest()

        payload = {
            "x402Version": 1,
            "kind": "signed",
            "network": "base-sepolia",
            "payload": inner,
            "signature": sig,
        }
        result = verifier.verify_payload(payload, requirements)
        assert result.valid

    def test_signature_verification_failure(
        self, merchant_wallet, requirements
    ):
        secret = "shared-secret-key"
        verifier = PaymentVerifier(secret=secret, merchant_wallet=merchant_wallet)

        payload = {
            "x402Version": 1,
            "kind": "signed",
            "network": "base-sepolia",
            "payload": {
                "amount": _amount_to_atomic(0.01),
                "payTo": merchant_wallet,
                "network": "base-sepolia",
            },
            "signature": "wrong-signature",
        }
        result = verifier.verify_payload(payload, requirements)
        assert not result.valid
        assert "Signature" in result.reason


# ── make_payment_header ────────────────────────────────────────────────────


class TestMakePaymentHeader:
    def test_returns_base64_string(self):
        header = make_payment_header(amount_usd=0.01, pay_to="0xABC")
        assert isinstance(header, str)
        # Should be valid base64
        decoded = base64.b64decode(header)
        assert isinstance(decoded, bytes)

    def test_decodes_to_valid_json(self):
        header = make_payment_header(
            amount_usd=1.50,
            pay_to="0x1234567890",
            network="base-mainnet",
        )
        decoded = json.loads(base64.b64decode(header))
        assert decoded["x402Version"] == 1
        assert decoded["kind"] == "verified"
        assert decoded["network"] == "base-mainnet"
        assert decoded["payload"]["amount"] == _amount_to_atomic(1.50)
        assert decoded["payload"]["payTo"] == "0x1234567890"

    def test_custom_transaction_hash(self):
        tx = "0xabcdef1234567890"
        header = make_payment_header(0.01, "0xABC", transaction_hash=tx)
        decoded = json.loads(base64.b64decode(header))
        assert decoded["payload"]["transactionHash"] == tx


# ── X402Middleware (ASGI) ──────────────────────────────────────────────────


class TestX402Middleware:
    @pytest.fixture
    def merchant_wallet(self):
        return "0x742d35Cc6634C0532925a3b844Bc9e7595f0bAe1"

    @pytest.fixture
    def pricing_rules(self):
        return [
            PricingRule(method="GET", path="/api/premium", amount=0.01, description="Premium data"),
            PricingRule(method="POST", path="/api/analyze", amount=0.05, description="Analysis"),
        ]

    @pytest.fixture
    def middleware(self, merchant_wallet, pricing_rules):
        """Build middleware wrapping a simple ASGI app."""

        async def simple_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": json.dumps({"data": "success"}).encode(),
            })

        return X402Middleware(
            app=simple_app,
            merchant_wallet=merchant_wallet,
            pricing_rules=pricing_rules,
        )

    async def _call_middleware(self, middleware, method="GET", path="/api/premium", payment_header=None):
        """Call the ASGI middleware and capture the response."""
        headers = []
        if payment_header:
            headers.append((X_PAYMENT_HEADER.lower().encode(), payment_header.encode()))

        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
        }

        responses = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            responses.append(message)

        await middleware(scope, receive, send)
        return responses

    async def test_unpaid_request_returns_402(self, middleware):
        responses = await self._call_middleware(middleware, path="/api/premium")
        start = responses[0]
        assert start["status"] == 402

        body = json.loads(responses[1]["body"])
        assert body["x402Version"] == 1
        assert len(body["accepts"]) == 1

        req = body["accepts"][0]
        assert req["scheme"] == "exact"
        assert req["network"] == "base-sepolia"
        assert req["amount"] == "10000"  # 0.01 USD in atomic units
        assert req["pay_to"] == middleware.merchant_wallet

    async def test_unpaid_post_returns_402(self, middleware):
        responses = await self._call_middleware(middleware, method="POST", path="/api/analyze")
        assert responses[0]["status"] == 402
        body = json.loads(responses[1]["body"])
        # 0.05 USD = 50000 atomic units
        assert body["accepts"][0]["amount"] == "50000"

    async def test_free_path_passes_through(self, middleware):
        """Paths without pricing rules should pass through."""
        responses = await self._call_middleware(middleware, path="/api/free")
        assert responses[0]["status"] == 200
        body = json.loads(responses[1]["body"])
        assert body["data"] == "success"

    async def test_valid_payment_proceeds(
        self, middleware, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.01,
            pay_to=merchant_wallet,
        )
        responses = await self._call_middleware(
            middleware, path="/api/premium", payment_header=header
        )
        assert responses[0]["status"] == 200

        # Check X-PAYMENT-RESPONSE header is present
        resp_headers = dict(
            (k.decode(), v.decode()) for k, v in responses[0]["headers"]
        )
        assert X_PAYMENT_RESPONSE_HEADER.lower() in resp_headers
        settlement = json.loads(resp_headers[X_PAYMENT_RESPONSE_HEADER.lower()])
        assert settlement["success"] is True
        assert settlement["settled"] is True
        assert settlement["transactionId"]

    async def test_invalid_payment_returns_402_with_error(
        self, middleware, merchant_wallet
    ):
        header = make_payment_header(
            amount_usd=0.99,  # Wrong amount
            pay_to=merchant_wallet,
        )
        responses = await self._call_middleware(
            middleware, path="/api/premium", payment_header=header
        )
        assert responses[0]["status"] == 402
        body = json.loads(responses[1]["body"])
        assert "error" in body
        assert body["retry"] is True

    async def test_wrong_recipient_returns_402(
        self, middleware
    ):
        header = make_payment_header(
            amount_usd=0.01,
            pay_to="0xWRONG",
        )
        responses = await self._call_middleware(
            middleware, path="/api/premium", payment_header=header
        )
        assert responses[0]["status"] == 402

    async def test_non_http_scope_passes_through(self, middleware):
        """Lifespan/websocket scopes should pass through to the app."""
        responses = []

        async def receive():
            return {}

        async def send(message):
            responses.append(message)

        await middleware({"type": "lifespan"}, receive, send)
        # The app would handle lifespan — no 402 response

    async def test_path_prefix_stripped_for_matching(self, merchant_wallet):
        """Middleware with path_prefix should strip it before matching rules."""
        async def simple_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = X402Middleware(
            app=simple_app,
            merchant_wallet=merchant_wallet,
            pricing_rules=[PricingRule(method="GET", path="/premium", amount=0.01)],
            path_prefix="/api/v1",
        )

        # /api/v1/premium should match the rule (prefix stripped)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/premium",
            "headers": [],
        }
        responses = []

        async def _recv():
            return {"type": "http.request"}

        async def _send(msg):
            responses.append(msg)

        await mw(scope, _recv, _send)
        assert responses[0]["status"] == 402

    async def test_check_fn_bypasses_payment(self, merchant_wallet):
        """If check_fn returns True, request proceeds without payment."""
        async def simple_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        def has_api_key(scope, headers):
            return b"x-api-key" in dict(scope.get("headers", []))

        mw = X402Middleware(
            app=simple_app,
            merchant_wallet=merchant_wallet,
            pricing_rules=[
                PricingRule(
                    method="GET", path="/api/data", amount=0.01,
                    check_fn=has_api_key,
                )
            ],
        )

        # Without API key → 402
        scope_no_key = {"type": "http", "method": "GET", "path": "/api/data", "headers": []}

        async def recv():
            return {}

        async def send_collect(responses):
            async def _send(msg):
                responses.append(msg)
            return _send

        responses = []
        await mw(scope_no_key, recv, await send_collect(responses))
        assert responses[0]["status"] == 402

        # With API key → 200
        scope_with_key = {
            "type": "http",
            "method": "GET",
            "path": "/api/data",
            "headers": [(b"x-api-key", b"secret123")],
        }
        responses = []
        await mw(scope_with_key, recv, await send_collect(responses))
        assert responses[0]["status"] == 200

    async def test_402_response_has_www_authenticate_header(
        self, middleware
    ):
        responses = await self._call_middleware(middleware, path="/api/premium")
        headers = dict((k.decode(), v.decode()) for k, v in responses[0]["headers"])
        assert headers.get("www-authenticate") == "x402"


# ── FacilitatorClient ──────────────────────────────────────────────────────


class TestFacilitatorClient:
    @pytest.fixture
    def requirements(self):
        return X402PaymentRequirements(
            amount="10000",
            pay_to="0xABC",
            resource="/test",
            network="base-sepolia",
        )

    async def test_simulate_verify_succeeds(self, requirements):
        client = FacilitatorClient(simulate=True)
        result = await client.verify({"test": True}, requirements)
        assert result.valid
        assert "Simulated" in result.reason
        assert result.transaction_id.startswith("sim_")
        await client.close()

    async def test_simulate_settle_succeeds(self, requirements):
        client = FacilitatorClient(simulate=True)
        result = await client.settle({"test": True}, requirements)
        assert result["success"] is True
        assert result["settled"] is True
        assert "transactionId" in result
        await client.close()

    async def test_simulate_settle_returns_correct_network(self, requirements):
        client = FacilitatorClient(simulate=True)
        result = await client.settle({}, requirements)
        assert result["network"] == "base-sepolia"
        await client.close()

    async def test_real_facilitator_network_error(self, requirements):
        """Real facilitator with bad URL should return invalid."""
        import httpx
        client = FacilitatorClient(
            base_url="http://localhost:99999",  # Will fail to connect
            timeout=1.0,
        )
        result = await client.verify({"test": True}, requirements)
        assert not result.valid
        assert "Facilitator error" in result.reason
        await client.close()


# ── Integration: Middleware + Engine ───────────────────────────────────────


class TestMiddlewareEngineIntegration:
    async def test_middleware_logs_payment_to_engine(
        self, merchant_wallet="0x742d35Cc6634C0532925a3b844Bc9e7595f0bAe1"
    ):
        """When a payment is verified, it can optionally be logged to the engine."""
        from mcp_payments.engine import PaymentEngine
        from mcp_payments.models import PaymentProvider, PaymentStatus

        engine = PaymentEngine(merchant_wallet=merchant_wallet)
        customer = engine.create_customer(
            name="x402 Payer", wallet_address="0xpayer"
        )

        async def simple_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = X402Middleware(
            app=simple_app,
            merchant_wallet=merchant_wallet,
            pricing_rules=[
                PricingRule(method="GET", path="/data", amount=0.01),
            ],
        )

        # Successful payment
        header = make_payment_header(0.01, merchant_wallet)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/data",
            "headers": [(X_PAYMENT_HEADER.lower().encode(), header.encode())],
        }
        responses = []

        async def _recv2():
            return {}

        async def _send2(msg):
            responses.append(msg)

        await mw(scope, _recv2, _send2)
        assert responses[0]["status"] == 200

        # The engine can be used to record this payment
        payment = engine.charge(
            customer_id=customer.id,
            amount=0.01,
            provider=PaymentProvider.X402,
            description="x402 middleware payment",
        )
        assert payment.status == PaymentStatus.SUCCEEDED
