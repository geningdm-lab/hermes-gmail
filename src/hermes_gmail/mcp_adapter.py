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
from typing import Annotated, Any

import anyio.to_thread
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from hermes_gmail.client import GateClient
from hermes_gmail.errors import GateError

READ_ONLY = ToolAnnotations(read_only_hint=True)


class Approval(BaseModel):
    """No fields: the owner answers yes or no to the preview's summary."""


class NotAsked(BaseModel):
    """The client cannot ask the owner. Never an approval: only `Approval` data is."""


def owner_approved(answer: Any) -> bool:
    """Only an answer the owner gave counts. The SDK wraps any value a resolver returns
    itself as `AcceptedElicitation(data=value)`, so the data's type is checked too."""
    return isinstance(answer, AcceptedElicitation) and isinstance(answer.data, Approval)


INSTRUCTIONS = (
    "Gmail for the owner's one account, through the hermes-gmail gate. Reads are bounded: "
    "check `complete` and continue with `next_page_token` when a result was cut short. "
    "Each error names a category; `refused` means fix the request, `gmail_quota` means wait, "
    "`oauth_invalid_grant` means the owner must reauthorize."
)


def build_server(client: GateClient) -> MCPServer:
    # The owner's answer may ride the client's retry (protocol 2026-07-28); sealing it with
    # a process-local key keeps a client from presenting an answer that was never given.
    server = MCPServer(
        name="hermes-gmail", instructions=INSTRUCTIONS, request_state_security=RequestStateSecurity.ephemeral()
    )

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

    # Writes carry no read-only hint, so Hermes asks the owner before each call. An apply
    # also asks on its own, with the summary the gate stored at prepare time.

    async def ask_owner(preview_id: str, ctx: Context) -> Elicit[Approval] | NotAsked:
        """The question shows the summary the gate stored at prepare time, not the agent's words.
        A client that cannot ask the owner gets `declined`, the same as a no."""
        capabilities = ctx.client_capabilities
        elicitation = capabilities.elicitation if capabilities is not None else None
        if elicitation is None or (elicitation.form is None and elicitation.url is not None):
            return NotAsked()
        preview = await call("preview", preview_id=preview_id)
        return Elicit(f"{preview['summary']} Approve this change?", Approval)

    Answer = Annotated[ElicitationResult[Approval], Resolve(ask_owner)]

    async def approved(op: str, preview_id: str, answer: Any) -> dict[str, Any]:
        if not owner_approved(answer):
            raise ToolError("declined: the owner did not approve, or could not be asked; nothing was written")
        return await call(op, preview_id=preview_id, approval="elicitation")

    @server.tool(description="Create a user label, or return the existing one with that name.")
    async def label_create(name: str) -> dict[str, Any]:
        return await call("label_create", name=name)

    @read_tool(
        "Preview a label change on up to 1000 messages, by `ids` or by `query`. `add` and `remove` are "
        "user label names or INBOX, UNREAD, STARRED, IMPORTANT. Returns a `preview_id` for modify_apply."
    )
    async def modify_prepare(
        ids: list[str] | None = None,
        query: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        return await call("modify_prepare", ids=ids, query=query, add=add, remove=remove)

    @server.tool(description="Apply a label-change preview after the owner approves it. Stops if the messages changed.")
    async def modify_apply(preview_id: str, answer: Answer) -> dict[str, Any]:
        return await approved("modify_apply", preview_id, answer)

    @read_tool(
        "Preview a new Gmail filter. `criteria` uses Gmail's filter fields (from, to, subject, query, ...); "
        "`add` is at most one user label plus STARRED or IMPORTANT; `remove` is INBOX, UNREAD, IMPORTANT or SPAM."
    )
    async def filter_prepare(
        criteria: dict[str, Any], add: list[str] | None = None, remove: list[str] | None = None
    ) -> dict[str, Any]:
        return await call("filter_prepare", criteria=criteria, add=add, remove=remove)

    @server.tool(description="Create the previewed filter after the owner approves it.")
    async def filter_apply(preview_id: str, answer: Answer) -> dict[str, Any]:
        return await approved("filter_apply", preview_id, answer)

    @read_tool("Preview deleting a filter by ID.")
    async def filter_delete_prepare(filter_id: str) -> dict[str, Any]:
        return await call("filter_delete_prepare", filter_id=filter_id)

    @server.tool(description="Delete the previewed filter after the owner approves it.")
    async def filter_delete_apply(preview_id: str, answer: Answer) -> dict[str, Any]:
        return await approved("filter_delete_apply", preview_id, answer)

    @server.tool(
        description=(
            "Create or update a plain-text draft. With `thread_id`, the thread's existing draft is updated and the "
            "reply headers are set. Nothing is sent."
        )
    )
    async def draft_put(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        thread_id: str | None = None,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        return await call("draft_put", to=to, subject=subject, body=body, cc=cc, thread_id=thread_id, draft_id=draft_id)

    return server


def run(socket_path: str | None = None, caller: str | None = None) -> int:
    path = socket_path or os.environ.get("HERMES_GMAIL_SOCKET")
    if not path:
        raise SystemExit("hermes-gmail mcp: set HERMES_GMAIL_SOCKET or pass --socket")
    name = caller or os.environ.get("HERMES_GMAIL_CALLER") or None
    build_server(GateClient(path, caller=name)).run("stdio")
    return 0
