"""CLI for mcp-payments."""
from __future__ import annotations

import json as json_lib
import sys

import click
from rich.console import Console
from rich.table import Table

from ..engine import PaymentEngine
from ..models import Currency, PaymentProvider, PaymentStatus, PricingModel

console = Console()


def get_engine() -> PaymentEngine:
    return PaymentEngine()


@click.group()
@click.version_option()
def cli():
    """mcp-payments — Payment execution layer for AI agents."""
    pass


@cli.command()
@click.argument("tool_name")
@click.argument("amount", type=float)
@click.option("--currency", default="USD", help="Currency code")
@click.option("--model", "pricing_model", default="per_use", help="Pricing model")
@click.option("--free-tier", type=int, help="Free calls before pricing")
@click.option("--description", "-d", default="", help="Price description")
def price(tool_name, amount, currency, pricing_model, free_tier, description):
    """Set pricing for a tool."""
    engine = get_engine()
    pricing = engine.set_price(
        tool_name=tool_name,
        amount=amount,
        currency=Currency(currency),
        pricing_model=PricingModel(pricing_model),
        free_tier_limit=free_tier,
        description=description,
    )
    console.print(f"[green]✓[/] Price set: {pricing.tool_name} = {pricing.price.display()} ({pricing_model})")
    if free_tier:
        console.print(f"  Free tier: {free_tier} calls")


@cli.command(name="price-list")
def price_list():
    """List all tool pricing."""
    engine = get_engine()
    prices = engine.list_prices()
    if not prices:
        console.print("[yellow]No pricing set[/]")
        return

    table = Table(title="Tool Pricing")
    table.add_column("Tool", style="cyan")
    table.add_column("Price", justify="right")
    table.add_column("Model")
    table.add_column("Free Tier", justify="right")
    for p in prices:
        table.add_row(
            p.tool_name,
            p.price.display(),
            p.price.pricing_model.value,
            str(p.free_tier_limit or "-"),
        )
    console.print(table)


@cli.command()
@click.option("--name", "-n", default="", help="Customer name")
@click.option("--agent-id", "-a", help="External agent ID")
@click.option("--wallet", "-w", help="Crypto wallet address")
@click.option("--email", "-e", help="Email")
def register(name, agent_id, wallet, email):
    """Register a new customer."""
    engine = get_engine()
    customer = engine.create_customer(name=name, agent_id=agent_id, wallet_address=wallet, email=email)
    console.print(f"[green]✓[/] Customer created: {customer.id}")
    console.print(f"  Name: {customer.name or '(none)'}")
    if customer.agent_id:
        console.print(f"  Agent ID: {customer.agent_id}")
    if customer.wallet_address:
        console.print(f"  Wallet: {customer.wallet_address}")


@cli.command(name="customer-info")
@click.argument("customer_id")
def customer_info(customer_id):
    """Show customer details."""
    engine = get_engine()
    customer = engine.get_customer(customer_id)
    if not customer:
        console.print(f"[red]Customer not found: {customer_id}[/]")
        sys.exit(1)
    console.print(f"[cyan]Customer:[/] {customer.id}")
    console.print(f"  Name: {customer.name or '(none)'}")
    console.print(f"  Balance: ${customer.balance:.2f}")
    if customer.agent_id:
        console.print(f"  Agent ID: {customer.agent_id}")
    if customer.wallet_address:
        console.print(f"  Wallet: {customer.wallet_address}")


@cli.command(name="top-up")
@click.argument("customer_id")
@click.argument("amount", type=float)
def top_up(customer_id, amount):
    """Add balance to a customer account."""
    engine = get_engine()
    customer = engine.top_up_balance(customer_id, amount)
    if not customer:
        console.print(f"[red]Customer not found: {customer_id}[/]")
        sys.exit(1)
    console.print(f"[green]✓[/] Topped up ${amount:.2f} → Balance: ${customer.balance:.2f}")


@cli.command()
@click.argument("customer_id")
@click.argument("amount", type=float)
@click.option("--currency", default="USD")
@click.option("--tool", "-t", help="Tool name")
@click.option("--description", "-d", default="")
@click.option("--provider", default="internal", type=click.Choice([p.value for p in PaymentProvider]))
def charge(customer_id, amount, currency, tool, description, provider):
    """Charge a customer."""
    engine = get_engine()
    try:
        payment = engine.charge(
            customer_id=customer_id,
            amount=amount,
            currency=Currency(currency),
            tool_name=tool,
            description=description,
            provider=PaymentProvider(provider),
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    if payment.status == PaymentStatus.SUCCEEDED:
        console.print(f"[green]✓[/] Charged ${amount:.2f} — Payment: {payment.id}")
    elif payment.status == PaymentStatus.FAILED:
        console.print(f"[red]✗ Failed: {payment.failure_reason}[/]")
    else:
        console.print(f"[yellow]⟳[/] Payment {payment.id}: {payment.status.value}")


@cli.command(name="payment-info")
@click.argument("payment_id")
def payment_info(payment_id):
    """Show payment details."""
    engine = get_engine()
    payment = engine.storage.get_payment(payment_id)
    if not payment:
        console.print(f"[red]Payment not found: {payment_id}[/]")
        sys.exit(1)

    status_colors = {"succeeded": "green", "failed": "red", "pending": "yellow", "processing": "yellow"}
    color = status_colors.get(payment.status.value, "white")
    console.print(f"[{color}]●[/] Payment: {payment.id}")
    console.print(f"  Status: [{color}]{payment.status.value}[/]")
    console.print(f"  Amount: {payment.amount:.2f} {payment.currency.value}")
    console.print(f"  Customer: {payment.customer_id}")
    console.print(f"  Provider: {payment.provider.value}")
    if payment.tool_name:
        console.print(f"  Tool: {payment.tool_name}")
    if payment.failure_reason:
        console.print(f"  Failure: {payment.failure_reason}")
    if payment.provider_transaction_id:
        console.print(f"  TX ID: {payment.provider_transaction_id}")


@cli.command()
@click.argument("payment_id")
@click.option("--amount", type=float, help="Partial refund amount")
@click.option("--reason", "-r", default="")
def refund(payment_id, amount, reason):
    """Refund a payment."""
    engine = get_engine()
    try:
        refund = engine.refund(payment_id=payment_id, amount=amount, reason=reason)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)
    console.print(f"[green]✓[/] Refunded ${refund.amount:.2f} — Refund: {refund.id}")


@cli.command(name="verify")
@click.argument("payment_id")
def verify(payment_id):
    """Verify a payment."""
    engine = get_engine()
    result = engine.verify_payment(payment_id)
    if result.get("valid"):
        console.print(f"[green]✓ Valid[/] Payment {payment_id}")
        console.print(f"  Amount: {result['amount']:.2f} {result['currency']}")
        console.print(f"  Provider: {result['provider']}")
    else:
        console.print(f"[red]✗ Invalid[/] {result.get('reason', 'Payment not found')}")


@cli.command()
@click.argument("payment_id")
def receipt(payment_id):
    """Generate a receipt for a payment."""
    engine = get_engine()
    receipt = engine.get_receipt(payment_id)
    if not receipt:
        console.print(f"[red]No completed payment found: {payment_id}[/]")
        sys.exit(1)
    console.print_json(data=receipt.model_dump(mode="json"))


@cli.command(name="payments")
@click.option("--customer", "-c", help="Filter by customer ID")
@click.option("--status", "-s", help="Filter by status")
@click.option("--limit", "-l", default=20, type=int)
def payments(customer, status, limit):
    """List payments."""
    engine = get_engine()
    status_enum = PaymentStatus(status) if status else None
    results = engine.storage.list_payments(customer_id=customer, status=status_enum, limit=limit)

    if not results:
        console.print("[yellow]No payments found[/]")
        return

    table = Table(title="Payments")
    table.add_column("ID", style="cyan")
    table.add_column("Amount", justify="right")
    table.add_column("Status")
    table.add_column("Tool")
    table.add_column("Created", style="dim")
    for p in results:
        status_colors = {"succeeded": "green", "failed": "red", "pending": "yellow"}
        color = status_colors.get(p.status.value, "white")
        table.add_row(
            p.id[:20] + "...",
            f"{p.amount:.2f}",
            f"[{color}]{p.status.value}[/]",
            p.tool_name or "-",
            p.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@cli.command()
@click.option("--customer", "-c", help="Filter by customer")
def summary(customer):
    """Payment analytics summary."""
    engine = get_engine()
    data = engine.summary(customer_id=customer)

    console.print("[cyan]Payment Summary[/]")
    console.print(f"  Total payments: {data['total_payments']}")
    console.print(f"  Succeeded: [green]{data['succeeded']}[/]")
    console.print(f"  Failed: [red]{data['failed']}[/]")
    console.print(f"  Volume: ${data['total_volume']:.2f}")
    console.print(f"  Refunded: ${data['total_refunded']:.2f}")
    console.print(f"  Net revenue: [green]${data['net_revenue']:.2f}[/]")
    if data["by_tool"]:
        console.print("\n[cyan]Revenue by Tool[/]")
        for tool, amt in sorted(data["by_tool"].items(), key=lambda x: -x[1]):
            console.print(f"  {tool}: ${amt:.2f}")


@cli.command()
@click.argument("amount", type=float)
@click.option("--resource-url", "-u", default="", help="URL requiring payment")
@click.option("--network", default="base-sepolia")
@click.option("--description", "-d", default="")
@click.option("--merchant-wallet", "-w", required=True, help="Your wallet address")
def x402(amount, resource_url, network, description, merchant_wallet):
    """Generate x402 payment requirements."""
    engine = PaymentEngine(merchant_wallet=merchant_wallet)
    req = engine.create_x402_requirements(
        amount=amount,
        currency=Currency.USD,
        resource_url=resource_url,
        description=description,
        network=network,
    )
    console.print("[cyan]x402 Payment Requirements[/]")
    console.print_json(data=req.model_dump())


@cli.command()
def tools():
    """List available MCP tools."""
    from ..server import MCPServer

    server = MCPServer()
    tool_list = server.list_tools()

    table = Table(title=f"MCP Tools ({len(tool_list)} available)")
    table.add_column("Tool", style="cyan")
    table.add_column("Description")
    for t in tool_list:
        desc = t["description"][:60] + "..." if len(t["description"]) > 60 else t["description"]
        table.add_row(t["name"], desc)
    console.print(table)


# ── v0.4.0: Usage Metering CLI ─────────────────────────────────────────────

@cli.command(name="meter")
@click.argument("customer_id")
@click.argument("tool_name")
@click.option("--unit", "-u", default="calls", type=click.Choice(["calls", "tokens", "input_tokens", "output_tokens", "seconds", "requests", "bytes", "custom"]), help="Usage unit")
@click.option("--quantity", "-q", type=float, default=1, help="Quantity consumed")
@click.option("--session", "-s", help="Session ID")
@click.option("--input-tokens", type=int, help="Input/prompt tokens (for token billing)")
@click.option("--output-tokens", type=int, help="Output/completion tokens (for token billing)")
def meter(customer_id, tool_name, unit, quantity, session, input_tokens, output_tokens):
    """Record a metered usage event."""
    from ..models import UsageUnit

    engine = get_engine()
    try:
        event = engine.record_usage(
            customer_id=customer_id,
            tool_name=tool_name,
            unit=UsageUnit(unit),
            quantity=quantity,
            session_id=session,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)
    console.print(f"[green]✓[/] Usage recorded: {event.id}")
    console.print(f"  Tool: {tool_name} | Unit: {unit} | Quantity: {event.quantity}")


@cli.command(name="usage")
@click.argument("customer_id")
@click.option("--tool", "-t", help="Filter by tool name")
def usage(customer_id, tool):
    """Show usage summary for a customer."""
    engine = get_engine()
    summary = engine.get_usage_summary(customer_id=customer_id, tool_name=tool)

    console.print(f"[cyan]Usage Summary[/] for {customer_id}")
    if tool:
        console.print(f"  Tool filter: {tool}")
    console.print(f"  Period: {summary.period_start.strftime('%Y-%m-%d')} → {summary.period_end.strftime('%Y-%m-%d')}")
    console.print(f"  Total events: {summary.total_events}")
    console.print(f"  Settled: [green]{summary.settled_events}[/] | Unsettled: [yellow]{summary.unsettled_events}[/]")
    if summary.total_by_unit:
        console.print("\n  [cyan]By Unit[/]")
        for unit, qty in summary.total_by_unit.items():
            console.print(f"    {unit}: {qty:,.0f}")
    console.print(f"\n  Estimated cost: [green]${summary.estimated_cost:.4f}[/]")


@cli.command(name="settle")
@click.argument("customer_id")
@click.option("--tool", "-t", help="Only settle a specific tool")
def settle(customer_id, tool):
    """Settle metered usage — charge for accumulated events."""
    engine = get_engine()
    result = engine.settle_usage(customer_id=customer_id, tool_name=tool)

    console.print(f"[cyan]Settlement Result[/]")
    console.print(f"  Events settled: {result.events_settled}")
    console.print(f"  Total charged: [green]${result.total_charged:.2f}[/]")
    if result.payment_ids:
        console.print(f"  Payment IDs: {', '.join(result.payment_ids[:5])}")
    if result.breakdown:
        console.print("\n  [cyan]Breakdown[/]")
        for tl, info in result.breakdown.items():
            status_color = "green" if info.get("charged") else "yellow"
            console.print(f"    {tl}: {info['events']} events → ${info['cost']:.4f} [{status_color}]{'charged' if info.get('charged') else 'no charge'}[/]")


if __name__ == "__main__":
    cli()
