"""Tests for storage persistence and edge cases."""
import json
import pytest
from mcp_payments.storage import Storage
from mcp_payments.models import Customer, Payment, PaymentIntent, PaymentStatus, ToolPricing, Price


@pytest.fixture
def storage(tmp_path):
    return Storage(data_dir=str(tmp_path / "payments"))


class TestStorage:
    def test_creates_data_dir(self, tmp_path):
        path = tmp_path / "new_dir"
        Storage(data_dir=str(path))
        assert path.exists()

    def test_customer_crud(self, storage):
        c = Customer(name="Test")
        storage.create_customer(c)
        assert storage.get_customer(c.id) is not None
        assert len(storage.list_customers()) == 1

    def test_payment_crud(self, storage):
        p = Payment(customer_id="cus_1", amount=100)
        storage.create_payment(p)
        assert storage.get_payment(p.id) is not None
        assert len(storage.list_payments()) == 1

    def test_payment_update(self, storage):
        p = Payment(customer_id="cus_1", amount=100)
        storage.create_payment(p)
        updated = storage.update_payment(p.id, status=PaymentStatus.SUCCEEDED)
        assert updated.status == PaymentStatus.SUCCEEDED

    def test_intent_crud(self, storage):
        i = PaymentIntent(customer_id="cus_1", amount=100)
        storage.create_intent(i)
        assert storage.get_intent(i.id) is not None

    def test_tool_pricing_crud(self, storage):
        tp = ToolPricing(tool_name="search", price=Price(amount=50))
        storage.set_tool_pricing(tp)
        assert storage.get_tool_pricing("search") is not None
        assert len(storage.list_tool_pricing()) == 1

    def test_tool_pricing_remove(self, storage):
        tp = ToolPricing(tool_name="search", price=Price(amount=50))
        storage.set_tool_pricing(tp)
        assert storage.remove_tool_pricing("search") is True
        assert storage.get_tool_pricing("search") is None

    def test_customer_balance_update(self, storage):
        c = Customer(name="Test", balance=100)
        storage.create_customer(c)
        storage.update_customer_balance(c.id, 50)
        assert storage.get_customer(c.id).balance == 150

    def test_payments_filter_by_customer(self, storage):
        p1 = Payment(customer_id="cus_1", amount=100)
        p2 = Payment(customer_id="cus_1", amount=200)
        p3 = Payment(customer_id="cus_2", amount=300)
        for p in [p1, p2, p3]:
            storage.create_payment(p)
        assert len(storage.list_payments(customer_id="cus_1")) == 2

    def test_payments_filter_by_status(self, storage):
        p1 = Payment(customer_id="cus_1", amount=100, status=PaymentStatus.SUCCEEDED)
        p2 = Payment(customer_id="cus_1", amount=200, status=PaymentStatus.FAILED)
        for p in [p1, p2]:
            storage.create_payment(p)
        assert len(storage.list_payments(status=PaymentStatus.SUCCEEDED)) == 1

    def test_persistence(self, tmp_path):
        storage1 = Storage(data_dir=str(tmp_path / "payments"))
        c = Customer(name="Persisted", balance=500)
        storage1.create_customer(c)

        # New instance loads from disk
        storage2 = Storage(data_dir=str(tmp_path / "payments"))
        loaded = storage2.get_customer(c.id)
        assert loaded is not None
        assert loaded.name == "Persisted"
        assert loaded.balance == 500

    def test_corrupt_db_handled(self, tmp_path):
        db_path = tmp_path / "payments" / "payments.json"
        db_path.parent.mkdir(parents=True)
        db_path.write_text("not json {{{")
        storage = Storage(data_dir=str(tmp_path / "payments"))
        # Should start fresh without crashing
        assert len(storage.list_customers()) == 0
