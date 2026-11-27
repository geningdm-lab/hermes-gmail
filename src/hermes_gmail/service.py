"""The socket service: one Unix socket, callers identified by peer credentials (D3).

Protocol: one JSON object per line each way. A request is
`{"op": name, "args": {...}, "caller": optional name, "account": optional address}`;
a response is `{"ok": true, "result": ...}` or `{"ok": false, "error": {"category", "detail"}}`.

The peer's user ID comes from the kernel, never from the request. When one user runs
several callers (same-user deployment, D4), the request names which one; the name can
only pick among that user's configured entries, never add to them.
"""

import inspect
import json
import os
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gmail.config import CallerConfig, Config
from hermes_gmail.errors import Category, GateError
from hermes_gmail.gate import Gate

REQUEST_MAX_BYTES = 4 * 1024 * 1024
_IDLE_TIMEOUT_SECONDS = 300.0

Operation = Callable[..., Any]


def read_operations(gate: Gate) -> dict[str, Operation]:
    reads = gate.reads
    return {
        "profile": reads.profile,
        "search": reads.search,
        "read_messages": reads.read_messages,
        "read_thread": reads.read_thread,
        "history": reads.history,
        "labels": reads.labels,
        "filters": reads.filters,
        "drafts": reads.drafts,
        "health": lambda force_refresh=False: gate.health(force_refresh=force_refresh is True),
    }


class Dispatcher:
    def __init__(self, gate: Gate, operations: dict[str, dict[str, Operation]]) -> None:
        """`operations` maps each role to its operations. An operation that needs the
        caller's name takes a `caller` keyword; the dispatcher fills it in."""
        self._gate = gate
        self._roles = operations
        self._callers = gate.config.callers

    def handle(self, peer_uid: int, line: bytes) -> dict[str, Any]:
        try:
            return {"ok": True, "result": self._dispatch(peer_uid, line)}
        except GateError as error:
            self._gate.health_counters.record(error)
            return {"ok": False, "error": error.to_dict()}
        except Exception as error:  # a bug, not a provider answer: say so without detail
            print(f"hermes-gmail: internal error {type(error).__name__}", file=sys.stderr)
            failure = self._gate.health_counters.fail(Category.GMAIL_FAILED, "the gate failed unexpectedly")
            return {"ok": False, "error": failure.to_dict()}

    def operations_for(self, caller: CallerConfig) -> dict[str, Operation]:
        allowed: dict[str, Operation] = {}
        for role in sorted(caller.roles):
            allowed.update(self._roles.get(role, {}))
        return allowed

    def _dispatch(self, peer_uid: int, line: bytes) -> Any:
        request = _parse(line)
        caller = self._caller(peer_uid, request.get("caller"))
        account = request.get("account")
        if account is not None and account != self._gate.config.account:
            raise GateError(Category.REFUSED, "this gate serves another account")
        name = request.get("op")
        operation = self.operations_for(caller).get(name) if isinstance(name, str) else None
        if operation is None:
            raise GateError(Category.REFUSED, "operation is unknown or not allowed for this caller")
        args = request.get("args", {})
        if not isinstance(args, dict):
            raise GateError(Category.REFUSED, "args must be an object")
        signature = inspect.signature(operation)
        if "caller" in signature.parameters:
            args = {**args, "caller": caller.name}
        try:
            signature.bind(**args)
        except TypeError:
            raise GateError(Category.REFUSED, "arguments do not match the operation") from None
        return operation(**args)

    def _caller(self, peer_uid: int, name: Any) -> CallerConfig:
        candidates = [c for c in self._callers if c.uid == peer_uid]
        if name is not None:
            candidates = [c for c in candidates if c.name == name]
        if len(candidates) != 1:
            raise GateError(Category.REFUSED, "caller is unknown, or the request must name which caller it is")
        return candidates[0]


def peer_uid(connection: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid, uid, gid}
        data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", data)[1]
    if hasattr(socket, "LOCAL_PEERCRED"):  # macOS and BSD: struct xucred {version, uid, ...}
        data = connection.getsockopt(0, socket.LOCAL_PEERCRED, struct.calcsize("IIh16I"))
        return struct.unpack_from("II", data)[1]
    raise OSError("this platform cannot identify a socket peer")


class _Handler(socketserver.StreamRequestHandler):
    server: GateServer
    timeout = _IDLE_TIMEOUT_SECONDS

    def handle(self) -> None:
        try:
            uid = peer_uid(self.connection)
        except OSError:
            return
        while True:
            line = self.rfile.readline(REQUEST_MAX_BYTES + 1)
            if not line:
                return
            if len(line) > REQUEST_MAX_BYTES:
                response: dict[str, Any] = {
                    "ok": False,
                    "error": GateError(Category.REFUSED, "request is too large").to_dict(),
                }
                self._send(response)
                return
            self._send(self.server.dispatcher.handle(uid, line))

    def _send(self, response: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        self.wfile.flush()


class GateServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, mode: int, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        _remove_stale_socket(path)
        previous = os.umask(0o177)  # no window where the socket is open to others
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(previous)
        os.chmod(path, mode)


def _remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise OSError("the socket path exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink()
    else:
        raise OSError("another service is listening on the socket")
    finally:
        probe.close()


def build_dispatcher(gate: Gate) -> Dispatcher:
    return Dispatcher(
        gate,
        {
            "read": read_operations(gate),
            "classify": {"classify_apply": gate.classifier.apply},
            "interactive": {},
        },
    )


def serve(config: Config) -> int:
    if config.socket is None:
        print("hermes-gmail: config has no socket path", file=sys.stderr)
        return 2
    gate = Gate(config)
    server = GateServer(config.socket, config.socket_mode, build_dispatcher(gate))

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"hermes-gmail: serving {len(config.callers)} callers", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        config.socket.unlink(missing_ok=True)
        gate.close()
    return 0


def _parse(line: bytes) -> dict[str, Any]:
    try:
        request = json.loads(line)
    except ValueError:
        raise GateError(Category.REFUSED, "request is not JSON") from None
    if not isinstance(request, dict):
        raise GateError(Category.REFUSED, "request must be a JSON object")
    return request
