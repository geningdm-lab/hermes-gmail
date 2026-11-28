"""A thin client for the gate's socket. Standard library only, for cron consumers."""

import json
import socket
from pathlib import Path
from typing import Any

from hermes_gmail.errors import Category, GateError

RESPONSE_MAX_BYTES = 64 * 1024 * 1024
# Longer than the service's longest operation deadline (classify_apply on a 2000-entry
# plan: 120 + 0.5 s per entry), so a slow run ends with its outcome, not `uncertain`.
DEFAULT_TIMEOUT_SECONDS = 1800.0


class GateClient:
    def __init__(
        self, socket_path: Path | str, *, caller: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._path = str(socket_path)
        self._caller = caller
        self._timeout = timeout

    def call(self, op: str, **args: Any) -> Any:
        """Run one operation. A failure raises GateError with the service's category."""
        request: dict[str, Any] = {"op": op, "args": args}
        if self._caller is not None:
            request["caller"] = self._caller
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout)
            try:
                connection.connect(self._path)
            except OSError:
                raise GateError(Category.GMAIL_FAILED, "the gate service is not reachable; nothing ran") from None
            try:
                connection.sendall(json.dumps(request).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline(RESPONSE_MAX_BYTES + 1)
            except OSError:
                # The service may have run the operation; only reads are safe to repeat.
                raise GateError(Category.UNCERTAIN, "the gate service did not answer") from None
        try:
            response = json.loads(line)
        except ValueError:
            raise GateError(Category.UNCERTAIN, "the gate service answered with something other than JSON") from None
        if not isinstance(response, dict):
            raise GateError(Category.UNCERTAIN, "the gate service answered with something other than an object")
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        try:
            category = Category(error.get("category"))
        except ValueError:
            category = Category.GMAIL_FAILED
        raise GateError(category, str(error.get("detail", "the gate refused without detail")))
