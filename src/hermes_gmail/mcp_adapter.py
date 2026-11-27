"""The agent adapter (D8): a stdio MCP server that Hermes starts with `trust: untrusted`.

It holds no credential: every tool call is one request to the gate's socket, so the caller
table, bounds and error categories are the service's. Read tools carry
`readOnlyHint: true`; anything that changes the mailbox carries no hint, so Hermes asks
the owner before running it.

Needs the `mcp` extra: `pip install hermes-gmail[mcp]`.
"""

import os
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from hermes_gmail.client import GateClient
from hermes_gmail.errors import GateError

READ_ONLY = ToolAnnotations(read_only_hint=True)

INSTRUCTIONS = (
    "Gmail for the owner's one account, through the hermes-gmail gate. Reads are bounded: "
    "check `complete` and continue with `next_page_token` when a result was cut short. "
    "Each error names a category; `refused` means fix the request, `gmail_quota` means wait, "
    "`oauth_invalid_grant` means the owner must reauthorize."
)


def build_server(client: GateClient) -> MCPServer:
    server = MCPServer(name="hermes-gmail", instructions=INSTRUCTIONS)

    async def call(op: str, **args: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(partial(client.call, op, **args))
        except GateError as error:
            raise ToolError(f"{error.category}: {error.detail}") from None

    def read_tool(description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return server.tool(description=description, annotations=READ_ONLY)

    @read_tool("The account address and current history ID.")
    async def profile() -> dict[str, Any]:
        return await call("profile")

    @read_tool(
        "Message IDs matching a Gmail search query. `page_bound` pages of up to 500 IDs (max 100); "
        "`complete` says whether every match was fetched."
    )
    async def search(query: str, page_bound: int = 1, page_token: str | None = None) -> dict[str, Any]:
        return await call("search", query=query, page_bound=page_bound, page_token=page_token)

    @read_tool(
        "Up to 100 messages by ID. `format`: minimal (labels), metadata (headers, snippet) or full "
        "(plain text, flagged `truncated` or `decode_failed`). Each ID is in `messages` or in `failed`."
    )
    async def read_messages(
        ids: list[str], format: str = "metadata", headers: list[str] | None = None, cap: int = 20000
    ) -> dict[str, Any]:
        return await call("read_messages", ids=ids, format=format, headers=headers, cap=cap)

    @read_tool("The latest `bound` messages of a thread with direction, reply headers and text.")
    async def read_thread(thread_id: str, bound: int = 20, cap: int = 20000) -> dict[str, Any]:
        return await call("read_thread", thread_id=thread_id, bound=bound, cap=cap)

    @read_tool("Mailbox changes since a history ID. Advance a cursor only when `complete` is true.")
    async def history(start_history_id: str, page_bound: int = 10, page_token: str | None = None) -> dict[str, Any]:
        return await call("history", start_history_id=start_history_id, page_bound=page_bound, page_token=page_token)

    @read_tool("All labels with ID, name and type.")
    async def labels() -> dict[str, Any]:
        return await call("labels")

    @read_tool("All Gmail filters with criteria and action.")
    async def filters() -> dict[str, Any]:
        return await call("filters")

    @read_tool("Drafts, optionally only those in one thread.")
    async def drafts(thread_id: str | None = None, page_bound: int = 10) -> dict[str, Any]:
        return await call("drafts", thread_id=thread_id, page_bound=page_bound)

    @read_tool("The gate's error counters, last successes, vault cooldown and budget use.")
    async def health() -> dict[str, Any]:
        return await call("health")

    return server


def run(socket_path: str | None = None, caller: str | None = None) -> int:
    path = socket_path or os.environ.get("HERMES_GMAIL_SOCKET")
    if not path:
        raise SystemExit("hermes-gmail mcp: set HERMES_GMAIL_SOCKET or pass --socket")
    name = caller or os.environ.get("HERMES_GMAIL_CALLER") or None
    build_server(GateClient(path, caller=name)).run("stdio")
    return 0
