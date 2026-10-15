"""A tiny stdio MCP server for tests: one tool per classification case.

Every mutating tool appends its name to the file named by $MCP_MARKER, so a
test can prove whether a call actually reached the server.
"""

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("inventory")


def _touch(name: str) -> None:
    marker = os.environ.get("MCP_MARKER")
    if marker:
        with Path(marker).open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_items() -> list[str]:
    """List inventory items."""
    return ["widget", "gadget"]


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
def delete_item(item: str) -> str:
    """Delete an item permanently."""
    _touch("delete_item")
    return f"deleted {item}"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def update_item(item: str, count: int) -> str:
    """Set an item's stock count."""
    _touch("update_item")
    return f"{item}={count}"


@server.tool()
def purge_cache() -> str:
    """Purge the cache (no annotations at all)."""
    _touch("purge_cache")
    return "purged"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def drop_table(table: str) -> str:
    """Claims to be read-only. Its name says otherwise."""
    _touch("drop_table")
    return f"dropped {table}"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def secret_tool() -> str:
    """Not on any allowlist."""
    return "secret"


if __name__ == "__main__":
    server.run("stdio")
