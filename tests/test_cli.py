"""Tests for CLI commands."""
import pytest
from click.testing import CliRunner
from mcp_payments.cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Redirect home so CLI uses temp storage."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestCLI:
    def test_version(self, runner, temp_home):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.1" in result.output

    def test_set_price(self, runner, temp_home):
        result = runner.invoke(cli, ["price", "search", "50"])
        assert result.exit_code == 0
        assert "search" in result.output

    def test_set_price_with_free_tier(self, runner, temp_home):
        result = runner.invoke(cli, ["price", "search", "50", "--free-tier", "10"])
        assert result.exit_code == 0
        assert "Free tier" in result.output

    def test_price_list_empty(self, runner, temp_home):
        result = runner.invoke(cli, ["price-list"])
        assert result.exit_code == 0

    def test_price_list_with_data(self, runner, temp_home):
        runner.invoke(cli, ["price", "search", "50"])
        runner.invoke(cli, ["price", "translate", "100"])
        result = runner.invoke(cli, ["price-list"])
        assert result.exit_code == 0
        assert "search" in result.output
        assert "translate" in result.output

    def test_register(self, runner, temp_home):
        result = runner.invoke(cli, ["register", "--name", "Test Agent"])
        assert result.exit_code == 0
        assert "Customer created" in result.output

    def test_register_with_wallet(self, runner, temp_home):
        result = runner.invoke(cli, [
            "register", "--name", "Agent", "--wallet", "0xABC",
        ])
        assert result.exit_code == 0
        assert "0xABC" in result.output

    def test_customer_info_not_found(self, runner, temp_home):
        result = runner.invoke(cli, ["customer-info", "cus_nonexistent"])
        assert result.exit_code == 1

    def test_top_up_and_balance(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        result = runner.invoke(cli, ["top-up", cid, "5000"])
        assert result.exit_code == 0
        assert "5000" in result.output

    def test_charge_success(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        result = runner.invoke(cli, ["charge", cid, "500"])
        assert result.exit_code == 0
        assert "Charged" in result.output

    def test_charge_insufficient(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        result = runner.invoke(cli, ["charge", cid, "99999"])
        assert result.exit_code == 0
        assert "Failed" in result.output

    def test_payments_list(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        runner.invoke(cli, ["charge", cid, "500"])
        result = runner.invoke(cli, ["payments"])
        assert result.exit_code == 0

    def test_summary(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        runner.invoke(cli, ["charge", cid, "500"])
        result = runner.invoke(cli, ["summary"])
        assert result.exit_code == 0
        assert "500" in result.output

    def test_verify(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        charge = runner.invoke(cli, ["charge", cid, "500"])
        pid = charge.output.split("Payment: ")[1].split("\n")[0].strip()
        result = runner.invoke(cli, ["verify", pid])
        assert result.exit_code == 0
        assert "Valid" in result.output

    def test_receipt(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        charge = runner.invoke(cli, ["charge", cid, "500"])
        pid = charge.output.split("Payment: ")[1].split("\n")[0].strip()
        result = runner.invoke(cli, ["receipt", pid])
        assert result.exit_code == 0

    def test_refund(self, runner, temp_home):
        reg = runner.invoke(cli, ["register", "--name", "Test"])
        cid = reg.output.split("Customer created: ")[1].split("\n")[0].strip()
        runner.invoke(cli, ["top-up", cid, "10000"])
        charge = runner.invoke(cli, ["charge", cid, "500"])
        pid = charge.output.split("Payment: ")[1].split("\n")[0].strip()
        result = runner.invoke(cli, ["refund", pid])
        assert result.exit_code == 0
        assert "Refunded" in result.output

    def test_tools_list(self, runner, temp_home):
        result = runner.invoke(cli, ["tools"])
        assert result.exit_code == 0
        assert "set_tool_price" in result.output

    def test_x402_requires_wallet(self, runner, temp_home):
        result = runner.invoke(cli, ["x402", "0.01"])
        assert result.exit_code != 0
