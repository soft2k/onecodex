"""End-to-end MCP stdio handshake and read-only tool call."""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    print("Starting MCP stdio smoke test", flush=True)
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "onecodex", "--config", str(root / "connections.json"), "serve"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as session:
            initialized = await session.initialize()
            listing = await session.list_tools()
            names = {tool.name for tool in listing.tools}
            assert {"get_metadata", "read_module", "stage_module", "apply_changes", "get_job",
                    "discover_bases", "import_base", "add_connection"} <= names
            result = await session.call_tool("list_connections", {})
            assert not result.isError, result
            print(f"MCP OK: {initialized.serverInfo.name}; {len(names)} tools; list_connections OK", flush=True)


async def bounded():
    async with asyncio.timeout(45):
        await main()


asyncio.run(bounded())
