"""MCP stdio transport — bridges payment tool handlers to the real MCP protocol.

This lets agents connect to mcp-payments via stdio transport (the standard MCP
transport used by Claude Desktop, Cursor, etc.).  We dynamically register all
69 tool definitions from the ``MCPServer`` class onto a ``FastMCP`` instance.

Usage::

    # In an MCP client config (e.g. Claude Desktop ``claude_desktop_config.json``):
    {
      "mcpServers": {
        "mcp-payments": {
          "command": "mcp-payments",
          "args": ["stdio"]
        }
      }
    }

    # Or programmatically:
    from mcp_payments.transport import run_stdio
    run_stdio()
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .engine import PaymentEngine
from .server import MCPServer, TOOL_DEFINITIONS

# JSON Schema property → Python type mapping
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _schema_to_annotation(prop_schema: dict[str, Any]) -> Any:
    """Convert a JSON-schema property to a Python type annotation."""
    json_type = prop_schema.get("type", "string")
    return _JSON_TYPE_MAP.get(json_type, str)


def _build_signature(input_schema: dict[str, Any]) -> inspect.Signature:
    """Build a Python function signature from an MCP input-schema dict."""
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    params: list[inspect.Parameter] = []

    for name, prop in properties.items():
        annotation = _schema_to_annotation(prop)
        if name in required:
            default = inspect.Parameter.empty
        else:
            default = prop.get("default", None)
        params.append(
            inspect.Parameter(
                name=name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )

    return inspect.Signature(parameters=params)


def _make_tool_function(
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
    inner_server: MCPServer,
) -> Any:
    """Create a callable with a proper signature that delegates to inner server."""
    sig = _build_signature(input_schema)

    def _handler(*args: Any, **kwargs: Any) -> str:
        result = inner_server.call_tool(tool_name, kwargs)
        return json.dumps(result, default=str)

    _handler.__name__ = tool_name
    _handler.__qualname__ = tool_name
    _handler.__doc__ = description
    _handler.__signature__ = sig  # type: ignore[attr-defined]

    return _handler


def create_fastmcp_server(
    engine: PaymentEngine | None = None,
) -> FastMCP:
    """Build a :class:`FastMCP` server with all payment tools registered.

    Parameters
    ----------
    engine
        Optional pre-configured :class:`PaymentEngine`.

    Returns
    -------
    FastMCP
        A server ready to ``.run(transport="stdio")``.
    """
    inner = MCPServer(engine=engine)
    fastmcp = FastMCP(
        name="mcp-payments",
        instructions=(
            "mcp-payments: complete payment infrastructure for AI agents. "
            "Set tool prices, register customers, charge for services, "
            "manage escrow, handle disputes, set up subscriptions, and "
            "deliver webhook events. The unified agent payment platform."
        ),
    )

    for tool_def in TOOL_DEFINITIONS:
        tool_name: str = tool_def["name"]
        description: str = tool_def["description"]
        input_schema: dict[str, Any] = tool_def.get("inputSchema", {})

        handler_fn = _make_tool_function(
            tool_name, description, input_schema, inner
        )

        fastmcp.add_tool(
            fn=handler_fn,
            name=tool_name,
            description=description,
        )

    return fastmcp


def run_stdio() -> None:
    """Run the MCP server on stdio transport (blocking)."""
    server = create_fastmcp_server()
    server.run(transport="stdio")
