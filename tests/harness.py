"""Offline stand-ins: Google (Gmail and the token endpoint) as a local HTTP server, 1Password
as a fake `op` executable, and a clock that tests move by hand. Every value here is synthetic.
"""

import json
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from hermes_gmail.config import BudgetConfig, Config, GoogleConfig, VaultConfig
from hermes_gmail.gate import Gate

ACCOUNT = "owner@example.test"
DOCUMENT = {
    "client_id": "synthetic-client-id",
    "client_secret": "SECRET-CLIENT-MARKER",
    "refresh_token": "SECRET-REFRESH-MARKER",
    "token_uri": "https://oauth2.googleapis.com/token",
}
# Strings that must never reach an error, a log line, a response or health().
SECRET_MARKERS = ("SECRET-CLIENT-MARKER", "SECRET-REFRESH-MARKER", "PROVIDER-BODY-MARKER", "VAULT-OUTPUT-MARKER")


@dataclass
class Reply:
    status: int = 200
    body: Any = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Seen:
    method: str
    path: str  # below /gmail/v1/users/me/, or "token"
    query: dict[str, list[str]]
    body: Any
    authorization: str | None


GmailHandler = Callable[[Seen], Reply]


class FakeGoogle:
    """Gmail under /gmail/v1/users/me/ and the token endpoint at /token.

    `token_replies` is consumed in order; when it is empty every refresh succeeds with a
    new access token. `gmail` answers each Gmail request.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: list[Seen] = []
        self.token_replies: list[Reply] = []
        self.issued = 0
        self.gmail: GmailHandler = lambda seen: Reply(404, {"error": {"code": 404}})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.01,), daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def requests(self, path: str | None = None) -> list[Seen]:
        with self.lock:
            return [s for s in self.seen if path is None or s.path == path]

    def gmail_requests(self) -> list[Seen]:
        with self.lock:
            return [s for s in self.seen if s.path != "token"]

    def _answer(self, seen: Seen) -> Reply:
        with self.lock:
            self.seen.append(seen)
            if seen.path == "token":
                if self.token_replies:
                    return self.token_replies.pop(0)
                self.issued += 1
                return Reply(200, {"access_token": f"access-{self.issued}", "expires_in": 3600})
        return self.gmail(seen)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if parts.path == "/token":
                    path, body = "token", dict(urllib.parse.parse_qsl(raw.decode()))
                elif parts.path.startswith("/gmail/v1/users/me/"):
                    path = parts.path.removeprefix("/gmail/v1/users/me/")
                    body = json.loads(raw) if raw else None
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                seen = Seen(
                    self.command, path, urllib.parse.parse_qs(parts.query), body, self.headers.get("Authorization")
                )
                reply = fake._answer(seen)
                payload = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        return Handler


class FakeOp:
    """An `op` executable whose behaviour the test sets per call: ok, throttle, fail or hang."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._state = directory / "op-state.json"
        self._log = directory / "op-calls.log"
        script = directory / "op"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time, pathlib\n"
            f"state = json.loads(pathlib.Path({str(self._state)!r}).read_text())\n"
            f"with open({str(self._log)!r}, 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "modes = state['modes']\n"
            "mode = modes.pop(0) if len(modes) > 1 else modes[0]\n"
            f"pathlib.Path({str(self._state)!r}).write_text(json.dumps(state))\n"
            "if mode == 'ok':\n"
            "    sys.stdout.write(state['document']); sys.exit(0)\n"
            "if mode == 'throttle':\n"
            "    sys.stderr.write('[ERROR] Too many requests VAULT-OUTPUT-MARKER\\n'); sys.exit(1)\n"
            "if mode == 'hang':\n"
            "    time.sleep(30)\n"
            "sys.stderr.write('[ERROR] unknown failure VAULT-OUTPUT-MARKER\\n'); sys.exit(1)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.set("ok")

    def set(self, *modes: str, document: dict[str, str] | str | None = None) -> None:
        """Each call takes the next mode; the last one repeats."""
        text = document if isinstance(document, str) else json.dumps(document or DOCUMENT)
        self._state.write_text(json.dumps({"modes": list(modes), "document": text}))

    def calls(self) -> list[list[str]]:
        if not self._log.exists():
            return []
        return [json.loads(line) for line in self._log.read_text().splitlines()]


class FakeClock:
    """Wall and monotonic time that move only when a test or a backoff sleeps."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self._now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class GateTestCase(unittest.TestCase):
    """A fresh fake Google, fake op, state directory and clock per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.google = FakeGoogle()
        self.op = FakeOp(self.tmp)
        self.clock = FakeClock()
        self.addCleanup(self.google.close)
        self.addCleanup(self._tmp.cleanup)

    def config(self, **vault: Any) -> Config:
        return Config(
            account=ACCOUNT,
            state_dir=self.tmp / "state",
            vault=VaultConfig(
                item="synthetic-item",
                vault="synthetic-vault",
                # An absolute path, so a real `op` on PATH can never be reached.
                op=str(self.op.directory / "op"),
                timeout_seconds=vault.pop("timeout_seconds", 5.0),
                attempts=vault.pop("attempts", 3),
                backoff_seconds=vault.pop("backoff_seconds", 1.0),
                cooldown_seconds=vault.pop("cooldown_seconds", 900.0),
            ),
            google=GoogleConfig(gmail_base=self.google.base, token_url=f"{self.google.base}/token"),
            budget=BudgetConfig(interval_seconds=0.0, in_flight=3),
        )

    def gate(self, **vault: Any) -> Gate:
        return Gate(self.config(**vault), wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)

    def assert_no_secret(self, *texts: object) -> None:
        for text in texts:
            rendered = text if isinstance(text, str) else json.dumps(text, default=str)
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, rendered)
