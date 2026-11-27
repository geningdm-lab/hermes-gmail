"""`hermes-gmail`: run the service or the agent adapter, or call a read operation.

Reads print the result as JSON on stdout. A failure prints `{"category", "detail"}` on
stderr and exits 1. The CLI offers no writes (D8): those go through the adapter, where
the owner approves them.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hermes_gmail.client import GateClient
from hermes_gmail.errors import GateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-gmail", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the gate service")
    serve.add_argument("--config", type=Path, required=True)

    mcp = commands.add_parser("mcp", help="run the agent adapter on stdio")
    mcp.add_argument("--socket")
    mcp.add_argument("--caller")

    def read(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--socket", default=os.environ.get("HERMES_GMAIL_SOCKET"))
        sub.add_argument("--caller", default=os.environ.get("HERMES_GMAIL_CALLER"))
        return sub

    read("profile", "account and history ID")
    health = read("health", "counters, cooldown and budget")
    health.add_argument("--force-refresh", action="store_true")
    search = read("search", "message IDs for a query")
    search.add_argument("--query", required=True)
    search.add_argument("--page-bound", type=int, default=1)
    search.add_argument("--page-token")
    messages = read("read-messages", "messages by ID")
    messages.add_argument("--ids", required=True, help="comma-separated")
    messages.add_argument("--format", default="metadata", choices=["minimal", "metadata", "full"])
    messages.add_argument("--headers", help="comma-separated header names")
    messages.add_argument("--cap", type=int, default=20000)
    thread = read("read-thread", "the latest messages of a thread")
    thread.add_argument("--thread-id", required=True)
    thread.add_argument("--bound", type=int, default=20)
    thread.add_argument("--cap", type=int, default=20000)
    history = read("history", "changes since a history ID")
    history.add_argument("--start-history-id", required=True)
    history.add_argument("--page-bound", type=int, default=10)
    history.add_argument("--page-token")
    read("labels", "all labels")
    read("filters", "all filters")
    drafts = read("drafts", "drafts, optionally for one thread")
    drafts.add_argument("--thread-id")
    return parser


def _read_call(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    command = args.command
    if command == "health":
        return "health", {"force_refresh": args.force_refresh}
    if command == "search":
        return "search", {"query": args.query, "page_bound": args.page_bound, "page_token": args.page_token}
    if command == "read-messages":
        headers = args.headers.split(",") if args.headers else None
        return "read_messages", {
            "ids": args.ids.split(","),
            "format": args.format,
            "headers": headers,
            "cap": args.cap,
        }
    if command == "read-thread":
        return "read_thread", {"thread_id": args.thread_id, "bound": args.bound, "cap": args.cap}
    if command == "history":
        return "history", {
            "start_history_id": args.start_history_id,
            "page_bound": args.page_bound,
            "page_token": args.page_token,
        }
    if command == "drafts":
        return "drafts", {"thread_id": args.thread_id}
    return command, {}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        from hermes_gmail.config import ConfigError, load_config
        from hermes_gmail.service import serve

        try:
            return serve(load_config(args.config))
        except ConfigError as error:
            print(f"hermes-gmail: {error}", file=sys.stderr)
            return 2
    if args.command == "mcp":
        from hermes_gmail.mcp_adapter import run

        return run(args.socket, args.caller)
    if not args.socket:
        print(
            json.dumps({"category": "refused", "detail": "set HERMES_GMAIL_SOCKET or pass --socket"}), file=sys.stderr
        )
        return 1
    op, call_args = _read_call(args)
    try:
        result = GateClient(args.socket, caller=args.caller).call(op, **call_args)
    except GateError as error:
        print(json.dumps(error.to_dict()), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
