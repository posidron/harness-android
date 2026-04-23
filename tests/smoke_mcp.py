"""Smoke test: instantiate the MCP server in-process and invoke a few tools
without going through stdio. Fast feedback when editing tool code.

Run with: poetry run python tests/smoke_mcp.py
"""
from __future__ import annotations

import asyncio
import json

from harness_android_mcp import mcp


async def main() -> None:
    # List tools
    tools = await mcp.list_tools()
    print(f"server: {mcp.name}  tools: {len(tools)}")
    for t in tools[:5]:
        print(f"  - {t.name}: {t.description.splitlines()[0] if t.description else ''}")
    print("  ...")

    # Invoke a safe read-only tool
    print("\n>> device_status")
    result = await mcp.call_tool("device_status", {})
    # FastMCP returns (content_list, structured_result)
    structured = result[1] if isinstance(result, tuple) else result
    print(json.dumps(structured, indent=2, default=str)[:600])

    print("\n>> cdp_status")
    result = await mcp.call_tool("cdp_status", {})
    structured = result[1] if isinstance(result, tuple) else result
    print(json.dumps(structured, indent=2, default=str)[:600])


if __name__ == "__main__":
    asyncio.run(main())
