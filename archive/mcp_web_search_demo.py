"""MCP SDK 2.3.0 example: call Tavily's web search tool directly."""

import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


SERVER_URL = "https://mcp.tavily.com/mcp/"


async def main():
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise SystemExit("Set the TAVILY_API_KEY environment variable before running this example.")

    query = sys.argv[1] if len(sys.argv) > 1 else "Python MCP SDK official documentation"

    # 1. The HTTP client handles networking and sends the API key in the auth header.
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx2.Timeout(30.0, read=120.0),
    ) as http_client:
        transport = streamable_http_client(SERVER_URL, http_client=http_client)

        # 2. Entering async with connects and initializes MCP; exiting closes the connection.
        async with Client(transport, read_timeout_seconds=120) as client:
            # 3. Ask the server which tools it provides and what arguments they accept.
            tools = []
            cursor = None
            while True:
                discovered = await client.list_tools(cursor=cursor)
                tools.extend(discovered.tools)
                cursor = discovered.next_cursor
                if cursor is None:
                    break
            print("Available tools:", [tool.name for tool in tools])

            # Use the name returned by the server, accepting underscores or hyphens.
            search_tool = next(
                (tool for tool in tools if tool.name in {"tavily_search", "tavily-search"}),
                None,
            )
            if search_tool is None:
                raise RuntimeError("No Tavily search tool found. Check the tool list above.")
            print("Search tool arguments:")
            print(json.dumps(search_tool.input_schema, ensure_ascii=False, indent=2))

            # 4. Specify the tool name and arguments; the MCP server runs the search.
            result = await client.call_tool(
                search_tool.name,
                arguments={"query": query, "max_results": 5, "search_depth": "basic"},
            )

            # 5. MCP returns a result object whose content contains a list of blocks.
            print("Search status:", "FAILED" if result.is_error else "SUCCESS")
            for block in result.content:
                if block.type == "text":
                    print(block.text)
            if result.is_error:
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
