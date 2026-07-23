"""Tests for v0.5.0 Service Marketplace Registry.

Tests the full lifecycle: register → publish → discover → purchase → review.
Also covers subscription plans and the unified discover+pay+provision flow.
"""
import pytest
from mcp_payments.engine import PaymentEngine
from mcp_payments.models import (
    PaymentStatus,
    ServiceStatus,
)
from mcp_payments.server import MCPServer
from mcp_payments.storage import Storage


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def engine(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    return PaymentEngine(storage=storage)


@pytest.fixture
def server(tmp_path):
    storage = Storage(data_dir=str(tmp_path / "payments"))
    engine = PaymentEngine(storage=storage)
    return MCPServer(engine=engine)


@pytest.fixture
def server_engine(tmp_path):
    """Shared engine + server for server tool tests."""
    storage = Storage(data_dir=str(tmp_path / "payments"))
    engine = PaymentEngine(storage=storage)
    server = MCPServer(engine=engine)
    return server, engine


@pytest.fixture
def provider(engine):
    """A service provider with prepaid balance."""
    c = engine.create_customer(name="Service Provider", agent_id="said:prov001")
    engine.top_up_balance(c.id, 100000)  # $1000
    return c


@pytest.fixture
def buyer(engine):
    """A customer who buys services."""
    c = engine.create_customer(name="Agent Buyer", agent_id="said:buy001")
    engine.top_up_balance(c.id, 50000)  # $500
    return c


@pytest.fixture
def published_service(engine, provider):
    """A service that's been registered and published."""
    svc = engine.register_service(
        name="Web Search API",
        slug="web-search",
        provider_customer_id=provider.id,
        description="Full-text web search for agents. Returns ranked results.",
        category="search",
        tags=["search", "web", "research"],
        price_per_call=5,  # 5 cents per call
        free_tier_limit=10,
        endpoint_url="https://api.example.com/v1/search",
        mcp_server_url="https://mcp.example.com/search",
        api_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    engine.publish_service(svc.id)
    return svc


# ── Service Registration ──────────────────────────────────────────────────────

class TestRegisterService:
    def test_register_creates_draft(self, engine, provider):
        svc = engine.register_service(
            name="Translation API",
            slug="translate",
            provider_customer_id=provider.id,
            description="Neural translation across 100 languages",
            category="translation",
            tags=["nlp", "translation"],
            price_per_call=2,
        )
        assert svc.id.startswith("svc_")
        assert svc.name == "Translation API"
        assert svc.slug == "translate"
        assert svc.status == ServiceStatus.DRAFT
        assert svc.price_per_call == 2
        assert svc.category == "translation"
        assert "nlp" in svc.tags

    def test_register_duplicate_slug_fails(self, engine, provider):
        engine.register_service(
            name="Service A",
            slug="unique-slug",
            provider_customer_id=provider.id,
        )
        with pytest.raises(ValueError, match="slug already taken"):
            engine.register_service(
                name="Service B",
                slug="unique-slug",
                provider_customer_id=provider.id,
            )

    def test_register_invalid_provider_fails(self, engine):
        with pytest.raises(ValueError, match="Provider customer not found"):
            engine.register_service(
                name="Ghost Service",
                slug="ghost",
                provider_customer_id="cus_nonexistent",
            )

    def test_register_with_all_pricing_models(self, engine, provider):
        svc = engine.register_service(
            name="LLM Inference",
            slug="llm-infer",
            provider_customer_id=provider.id,
            price_per_call=10,
            price_per_token=0.05,
            price_per_second=0.5,
        )
        assert svc.price_per_call == 10
        assert svc.price_per_token == 0.05
        assert svc.price_per_second == 0.5

    def test_register_free_service(self, engine, provider):
        svc = engine.register_service(
            name="Free Tool",
            slug="free-tool",
            provider_customer_id=provider.id,
            price_per_call=0,
            free_tier_limit=1000,
        )
        assert svc.price_per_call == 0
        assert svc.free_tier_limit == 1000


class TestPublishService:
    def test_publish_makes_active(self, engine, provider):
        svc = engine.register_service(
            name="My Service",
            slug="my-service",
            provider_customer_id=provider.id,
        )
        assert svc.status == ServiceStatus.DRAFT

        published = engine.publish_service(svc.id)
        assert published.status == ServiceStatus.ACTIVE

    def test_publish_nonexistent_returns_none(self, engine):
        result = engine.publish_service("svc_nonexistent")
        assert result is None


# ── Discovery ──────────────────────────────────────────────────────────────────

class TestServiceDiscovery:
    def test_search_finds_by_name(self, engine, published_service):
        results = engine.search_services("web search")
        assert len(results) >= 1
        assert results[0].slug == "web-search"

    def test_search_finds_by_tag(self, engine, published_service):
        results = engine.search_services("research")
        assert len(results) >= 1
        assert results[0].id == published_service.id

    def test_search_finds_by_category(self, engine, published_service):
        results = engine.search_services("search")
        assert any(s.slug == "web-search" for s in results)

    def test_search_ignores_drafts(self, engine, provider):
        engine.register_service(
            name="Secret Tool",
            slug="secret",
            provider_customer_id=provider.id,
        )
        results = engine.search_services("secret")
        assert len(results) == 0  # Draft shouldn't appear

    def test_search_empty_query_returns_nothing(self, engine, published_service):
        results = engine.search_services("")
        assert len(results) == 0

    def test_search_scoring(self, engine, provider):
        """Name match should score higher than description match."""
        engine.register_service(
            name="search engine",
            slug="search-engine",
            provider_customer_id=provider.id,
            description="A tool for finding things",
        )
        engine.publish_service(engine.get_service_by_slug("search-engine").id)

        engine.register_service(
            name="Data Tool",
            slug="data-tool",
            provider_customer_id=provider.id,
            description="search through databases",
        )
        engine.publish_service(engine.get_service_by_slug("data-tool").id)

        results = engine.search_services("search")
        # "search engine" (name match) should rank higher than "Data Tool" (description match)
        assert results[0].slug == "search-engine"

    def test_list_by_category(self, engine, published_service):
        results = engine.list_services(category="search", status="active")
        assert len(results) >= 1
        assert all(s.category == "search" for s in results)

    def test_list_by_tag(self, engine, published_service):
        results = engine.list_services(tag="research", status="active")
        assert len(results) >= 1

    def test_list_only_active_by_default(self, engine, provider):
        engine.register_service(
            name="Draft Service",
            slug="draft-svc",
            provider_customer_id=provider.id,
        )
        active = engine.list_services(status="active")
        drafts = engine.list_services(status="draft")
        assert all(s.status.value == "active" for s in active)
        assert all(s.status.value == "draft" for s in drafts)

    def test_get_by_slug(self, engine, published_service):
        svc = engine.get_service_by_slug("web-search")
        assert svc is not None
        assert svc.id == published_service.id

    def test_get_by_slug_nonexistent(self, engine):
        assert engine.get_service_by_slug("does-not-exist") is None


# ── Purchase: The discover → pay → provision flow ────────────────────────────

class TestPurchaseService:
    def test_purchase_charges_and_provisions(self, engine, published_service, buyer):
        result = engine.purchase_service(
            service_id=published_service.id,
            customer_id=buyer.id,
        )
        assert result["access_granted"] is True
        assert result["payment_status"] == "succeeded"
        assert result["amount_charged"] == 5  # price_per_call
        assert result["endpoint_url"] == "https://api.example.com/v1/search"
        assert result["mcp_server_url"] == "https://mcp.example.com/search"
        assert result["api_schema"] is not None

    def test_purchase_updates_service_metrics(self, engine, published_service, buyer):
        engine.purchase_service(published_service.id, buyer.id)
        engine.purchase_service(published_service.id, buyer.id)

        svc = engine.get_service(published_service.id)
        assert svc.total_calls == 2
        assert svc.total_revenue == 10  # 2 * 5 cents

    def test_purchase_custom_amount(self, engine, published_service, buyer):
        result = engine.purchase_service(
            service_id=published_service.id,
            customer_id=buyer.id,
            amount=20,  # override
        )
        assert result["amount_charged"] == 20

    def test_purchase_free_service(self, engine, provider, buyer):
        svc = engine.register_service(
            name="Free API",
            slug="free-api",
            provider_customer_id=provider.id,
            price_per_call=0,
        )
        engine.publish_service(svc.id)

        result = engine.purchase_service(svc.id, buyer.id)
        assert result["amount_charged"] == 0
        assert result["access_granted"] is True

    def test_purchase_insufficient_balance(self, engine, provider):
        poor_buyer = engine.create_customer(name="Poor Agent")
        # Don't top up — balance is 0

        svc = engine.register_service(
            name="Expensive API",
            slug="expensive",
            provider_customer_id=provider.id,
            price_per_call=999999,
        )
        engine.publish_service(svc.id)

        result = engine.purchase_service(svc.id, poor_buyer.id)
        assert result["access_granted"] is False
        assert result["payment_status"] == "failed"

    def test_purchase_nonexistent_service(self, engine, buyer):
        with pytest.raises(ValueError, match="Service not found"):
            engine.purchase_service("svc_nonexistent", buyer.id)

    def test_purchase_draft_service_fails(self, engine, provider, buyer):
        svc = engine.register_service(
            name="Unpublished",
            slug="unpublished",
            provider_customer_id=provider.id,
            price_per_call=1,
        )
        # Don't publish — still DRAFT
        with pytest.raises(ValueError, match="not available"):
            engine.purchase_service(svc.id, buyer.id)

    def test_purchase_registers_tool_pricing(self, engine, published_service, buyer):
        """Purchasing a service should auto-register its pricing for metering."""
        engine.purchase_service(published_service.id, buyer.id)
        pricing = engine.get_price("web-search")
        assert pricing is not None
        assert pricing.price.amount == 5

    def test_purchase_deducts_balance(self, engine, published_service, buyer):
        balance_before = engine.get_customer(buyer.id).balance
        engine.purchase_service(published_service.id, buyer.id)
        balance_after = engine.get_customer(buyer.id).balance
        assert balance_after == balance_before - 5


# ── Subscription Plans ──────────────────────────────────────────────────────────

class TestSubscriptionPlans:
    def test_create_plan(self, engine, published_service):
        plan = engine.create_plan(
            service_id=published_service.id,
            name="Pro",
            price_cents=1000,  # $10
            billing_interval="monthly",
            included_calls=1000,
            features=["priority support", "higher rate limits"],
            trial_days=7,
        )
        assert plan.id.startswith("plan_")
        assert plan.name == "Pro"
        assert plan.price_cents == 1000
        assert plan.included_calls == 1000
        assert plan.trial_days == 7
        assert "priority support" in plan.features

    def test_create_plan_nonexistent_service(self, engine):
        with pytest.raises(ValueError, match="Service not found"):
            engine.create_plan(
                service_id="svc_nonexistent",
                name="Pro",
                price_cents=1000,
            )

    def test_list_plans_by_service(self, engine, published_service):
        engine.create_plan(published_service.id, "Basic", 500)
        engine.create_plan(published_service.id, "Pro", 2000)

        plans = engine.list_plans(service_id=published_service.id)
        assert len(plans) == 2

    def test_subscribe_charges_immediately(self, engine, published_service, buyer):
        plan = engine.create_plan(
            service_id=published_service.id,
            name="Pro",
            price_cents=1000,
        )
        balance_before = engine.get_customer(buyer.id).balance

        result = engine.subscribe_to_plan(plan.id, buyer.id)
        assert result["subscribed"] is True
        assert result["amount_charged"] == 1000
        assert result["payment_status"] == "succeeded"

        balance_after = engine.get_customer(buyer.id).balance
        assert balance_after == balance_before - 1000

    def test_subscribe_free_plan(self, engine, published_service, buyer):
        plan = engine.create_plan(
            service_id=published_service.id,
            name="Free Tier",
            price_cents=0,
        )
        result = engine.subscribe_to_plan(plan.id, buyer.id)
        assert result["subscribed"] is True
        assert result["amount_charged"] == 0

    def test_subscribe_nonexistent_plan(self, engine, buyer):
        with pytest.raises(ValueError, match="Plan not found"):
            engine.subscribe_to_plan("plan_nonexistent", buyer.id)


# ── Reviews ──────────────────────────────────────────────────────────────────────

class TestServiceReviews:
    def test_review_verified_after_purchase(self, engine, published_service, buyer):
        # Buy first
        engine.purchase_service(published_service.id, buyer.id)
        # Then review
        review = engine.review_service(
            service_id=published_service.id,
            customer_id=buyer.id,
            rating=5,
            comment="Excellent search results!",
        )
        assert review.rating == 5
        assert review.verified is True

    def test_review_unverified_without_purchase(self, engine, published_service, provider):
        # Provider reviews their own service without buying
        review = engine.review_service(
            service_id=published_service.id,
            customer_id=provider.id,
            rating=4,
        )
        assert review.verified is False

    def test_review_updates_service_rating(self, engine, published_service, buyer, provider):
        engine.purchase_service(published_service.id, buyer.id)
        engine.review_service(published_service.id, buyer.id, rating=5)
        engine.review_service(published_service.id, provider.id, rating=3)

        svc = engine.get_service(published_service.id)
        assert svc.rating_count == 2
        assert svc.rating_sum == 8

    def test_review_invalid_rating(self, engine, published_service, buyer):
        with pytest.raises(ValueError, match="Rating must be"):
            engine.review_service(published_service.id, buyer.id, rating=0)

        with pytest.raises(ValueError, match="Rating must be"):
            engine.review_service(published_service.id, buyer.id, rating=6)

    def test_review_nonexistent_service(self, engine, buyer):
        with pytest.raises(ValueError, match="Service not found"):
            engine.review_service("svc_nonexistent", buyer.id, rating=5)

    def test_list_reviews_by_service(self, engine, published_service, buyer):
        engine.purchase_service(published_service.id, buyer.id)
        engine.review_service(published_service.id, buyer.id, rating=5, comment="Great")

        reviews = engine.list_reviews(service_id=published_service.id)
        assert len(reviews) == 1
        assert reviews[0].rating == 5
        assert reviews[0].comment == "Great"


# ── MCP Server Tool Integration ──────────────────────────────────────────────

class TestMarketplaceServerTools:
    def test_register_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        result = server.call_tool("register_service", {
            "name": "Test API",
            "slug": "test-api",
            "provider_customer_id": provider.id,
            "description": "A test service",
            "category": "testing",
            "price_per_call": 3,
        })
        assert "result" in result
        assert result["result"]["slug"] == "test-api"
        assert result["result"]["status"] == "draft"

    def test_publish_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        reg = server.call_tool("register_service", {
            "name": "Pub Test",
            "slug": "pub-test",
            "provider_customer_id": provider.id,
        })
        svc_id = reg["result"]["service_id"]

        result = server.call_tool("publish_service", {"service_id": svc_id})
        assert result["result"]["status"] == "active"

    def test_search_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        svc = engine.register_service(
            name="Web Search API",
            slug="web-search",
            provider_customer_id=provider.id,
            description="Full-text web search",
            category="search",
            tags=["search", "web"],
            price_per_call=5,
        )
        engine.publish_service(svc.id)

        result = server.call_tool("search_services", {"query": "web search"})
        assert result["result"]["count"] >= 1
        services = result["result"]["services"]
        assert any(s["slug"] == "web-search" for s in services)

    def test_purchase_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)
        buyer = engine.create_customer(name="Buyer")
        engine.top_up_balance(buyer.id, 50000)

        svc = engine.register_service(
            name="Search API",
            slug="search-api",
            provider_customer_id=provider.id,
            price_per_call=5,
            endpoint_url="https://api.example.com/v1/search",
        )
        engine.publish_service(svc.id)

        result = server.call_tool("purchase_service", {
            "service_id": svc.id,
            "customer_id": buyer.id,
        })
        assert result["result"]["access_granted"] is True
        assert result["result"]["endpoint_url"] == "https://api.example.com/v1/search"

    def test_review_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)
        buyer = engine.create_customer(name="Buyer")
        engine.top_up_balance(buyer.id, 50000)

        svc = engine.register_service(
            name="Reviewable API",
            slug="reviewable",
            provider_customer_id=provider.id,
            price_per_call=5,
        )
        engine.publish_service(svc.id)

        # Purchase first for verified review
        server.call_tool("purchase_service", {
            "service_id": svc.id,
            "customer_id": buyer.id,
        })
        result = server.call_tool("review_service", {
            "service_id": svc.id,
            "customer_id": buyer.id,
            "rating": 5,
            "comment": "Works great",
        })
        assert result["result"]["rating"] == 5
        assert result["result"]["verified"] is True

    def test_get_service_by_slug_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        svc = engine.register_service(
            name="Detail API",
            slug="detail-api",
            provider_customer_id=provider.id,
            price_per_call=5,
        )
        engine.publish_service(svc.id)

        result = server.call_tool("get_service", {"slug": "detail-api"})
        assert result["result"]["slug"] == "detail-api"
        assert result["result"]["price_per_call"] == 5

    def test_list_services_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        svc = engine.register_service(
            name="List API",
            slug="list-api",
            provider_customer_id=provider.id,
            category="search",
        )
        engine.publish_service(svc.id)

        result = server.call_tool("list_services", {"category": "search"})
        assert result["result"]["count"] >= 1

    def test_create_plan_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)

        svc = engine.register_service(
            name="Plan API",
            slug="plan-api",
            provider_customer_id=provider.id,
        )

        result = server.call_tool("create_plan", {
            "service_id": svc.id,
            "name": "Pro",
            "price_cents": 500,
            "included_calls": 100,
        })
        assert result["result"]["name"] == "Pro"
        assert result["result"]["price_cents"] == 500

    def test_subscribe_via_server(self, server_engine):
        server, engine = server_engine
        provider = engine.create_customer(name="Provider")
        engine.top_up_balance(provider.id, 100000)
        buyer = engine.create_customer(name="Buyer")
        engine.top_up_balance(buyer.id, 50000)

        svc = engine.register_service(
            name="Sub API",
            slug="sub-api",
            provider_customer_id=provider.id,
        )
        plan = server.call_tool("create_plan", {
            "service_id": svc.id,
            "name": "Basic",
            "price_cents": 200,
        })
        plan_id = plan["result"]["plan_id"]

        result = server.call_tool("subscribe_to_plan", {
            "plan_id": plan_id,
            "customer_id": buyer.id,
        })
        assert result["result"]["subscribed"] is True

    def test_unknown_tool_returns_error(self, server):
        result = server.call_tool("nonexistent_marketplace_tool", {})
        assert "error" in result


# ── Persistence ──────────────────────────────────────────────────────────────

class TestMarketplacePersistence:
    def test_services_persist_across_restart(self, tmp_path, provider):
        from mcp_payments.models import ServiceListing

        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)

        # Create provider in this engine
        prov = engine1.create_customer(name="Provider")
        svc = engine1.register_service(
            name="Persistent Service",
            slug="persist-svc",
            provider_customer_id=prov.id,
            price_per_call=5,
        )
        engine1.publish_service(svc.id)

        # Reload
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)

        found = engine2.get_service_by_slug("persist-svc")
        assert found is not None
        assert found.name == "Persistent Service"
        assert found.price_per_call == 5
        assert found.status == ServiceStatus.ACTIVE

    def test_plans_persist_across_restart(self, tmp_path):
        from mcp_payments.models import SubscriptionPlan

        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)
        prov = engine1.create_customer(name="Provider")
        svc = engine1.register_service(
            name="Service with Plans",
            slug="planned-svc",
            provider_customer_id=prov.id,
        )
        engine1.create_plan(svc.id, "Pro", 1000)

        # Reload
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        plans = engine2.list_plans(service_id=svc.id)
        assert len(plans) == 1
        assert plans[0].name == "Pro"

    def test_reviews_persist_across_restart(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        engine1 = PaymentEngine(storage=storage1)
        prov = engine1.create_customer(name="Provider")
        svc = engine1.register_service(
            name="Reviewed Service",
            slug="reviewed-svc",
            provider_customer_id=prov.id,
        )

        # Create buyer and purchase for verified review
        buyer = engine1.create_customer(name="Buyer")
        engine1.top_up_balance(buyer.id, 10000)
        engine1.publish_service(svc.id)
        engine1.purchase_service(svc.id, buyer.id)
        engine1.review_service(svc.id, buyer.id, rating=5, comment="Great")

        # Reload
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        engine2 = PaymentEngine(storage=storage2)
        reviews = engine2.list_reviews(service_id=svc.id)
        assert len(reviews) == 1
        assert reviews[0].rating == 5
        assert reviews[0].verified is True


# ── Full Lifecycle Integration ───────────────────────────────────────────────

class TestMarketplaceLifecycle:
    def test_full_provider_to_consumer_flow(self, engine):
        """End-to-end: provider registers → publishes → agent discovers →
        purchases → uses (metered) → settles → reviews."""
        # 1. Provider registers
        provider = engine.create_customer(name="API Provider")
        engine.top_up_balance(provider.id, 100000)

        svc = engine.register_service(
            name="Premium Data API",
            slug="premium-data",
            provider_customer_id=provider.id,
            description="High-quality data endpoints for agents",
            category="data",
            tags=["data", "premium", "api"],
            price_per_call=10,
            free_tier_limit=5,
            endpoint_url="https://data.example.com/v2",
            mcp_server_url="https://mcp.example.com/data",
        )
        assert svc.status == ServiceStatus.DRAFT

        # 2. Publish
        engine.publish_service(svc.id)
        assert engine.get_service(svc.id).status == ServiceStatus.ACTIVE

        # 3. Consumer discovers via search
        results = engine.search_services("data")
        assert len(results) >= 1
        assert results[0].slug == "premium-data"

        # 4. Consumer purchases
        consumer = engine.create_customer(name="Data Consumer")
        engine.top_up_balance(consumer.id, 50000)

        purchase = engine.purchase_service(svc.id, consumer.id)
        assert purchase["access_granted"] is True
        assert purchase["amount_charged"] == 10
        assert purchase["endpoint_url"] == "https://data.example.com/v2"

        # 5. Record metered usage
        engine.record_usage(
            customer_id=consumer.id,
            tool_name="premium-data",
            quantity=1,
        )
        engine.record_usage(
            customer_id=consumer.id,
            tool_name="premium-data",
            quantity=1,
        )

        summary = engine.get_usage_summary(customer_id=consumer.id, tool_name="premium-data")
        assert summary.total_events == 2

        # 6. Settle usage
        settlement = engine.settle_usage(customer_id=consumer.id, tool_name="premium-data")
        assert settlement.events_settled == 2

        # 7. Review the service
        review = engine.review_service(
            service_id=svc.id,
            customer_id=consumer.id,
            rating=5,
            comment="Excellent data quality",
        )
        assert review.verified is True  # Has successful payment

        # 8. Verify final state
        final_svc = engine.get_service(svc.id)
        assert final_svc.total_calls >= 1  # At least the initial purchase
        assert final_svc.rating_count == 1
        assert final_svc.rating_sum == 5
