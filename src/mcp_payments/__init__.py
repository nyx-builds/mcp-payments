"""
mcp-payments — Payment execution layer for AI agents.

Create, charge, and verify payments via MCP server, CLI, or REST API.
Supports x402 protocol, pricing schemas, and multi-currency.
"""

__version__ = "0.10.0"


def __getattr__(name):
    """Lazy imports to avoid circular dependencies."""
    if name == "create_fastmcp_server":
        from .transport import create_fastmcp_server
        return create_fastmcp_server
    if name == "run_stdio":
        from .transport import run_stdio
        return run_stdio
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
