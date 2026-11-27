"""Acceptance row 7: refusals happen before any vault or provider call; the reader cannot write."""

import dataclasses
import json
import os
import socket
import threading
import unittest

from hermes_gmail.client import GateClient
from hermes_gmail.config import CallerConfig
from hermes_gmail.errors import Category, GateError
from hermes_gmail.gate import Gate
from hermes_gmail.service import GateServer, build_dispatcher
from tests.harness import ACCOUNT, GateTestCase, Reply

UID = os.getuid()
READER = CallerConfig("reader", UID, frozenset({"read"}))
CLASSIFIER = CallerConfig("classifier", UID, frozenset({"read", "classify"}))
AGENT = CallerConfig("agent", UID, frozenset({"read", "interactive"}))


class ServiceTestCase(GateTestCase):
    def start(self, *callers):
        config = dataclasses.replace(self.config(), socket=self.tmp / "gate.sock", callers=callers)
        self.served = Gate(config, wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)
        self.addCleanup(self.served.close)
        self.dispatcher = build_dispatcher(self.served)
        server = GateServer(config.socket, 0o600, self.dispatcher)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.socket_path = config.socket

    def client(self, caller=None):
        return GateClient(self.socket_path, caller=caller, timeout=10)

    def raw(self, payload: bytes):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.socket_path))
            connection.sendall(payload + b"\n")
            with connection.makefile("rb") as stream:
                return json.loads(stream.readline())


class Service(ServiceTestCase):
    def test_a_configured_caller_reads_through_the_socket(self):
        self.google.gmail = lambda seen: Reply(200, {"emailAddress": ACCOUNT, "historyId": "7"})
        self.start(READER)
        self.assertEqual(self.client().call("profile"), {"account": ACCOUNT, "history_id": "7"})
        self.assertEqual(self.client("reader").call("health")["events"]["vault_reads"], 1)

    def test_refusals_happen_before_any_vault_or_provider_call(self):
        self.start(READER, CLASSIFIER)
        cases = [
            ("unknown caller name", lambda: self.client("nobody").call("profile")),
            ("two callers share the user; none named", lambda: self.client().call("profile")),
            (
                "operation outside the caller's set",
                lambda: self.client("reader").call("classify_apply", plan=[], rule_version="r1"),
            ),
            ("unknown operation", lambda: self.client("reader").call("request", path="messages/send")),
            ("raw endpoint", lambda: self.client("reader").call("transport")),
            ("bad arguments", lambda: self.client("reader").call("search", query="q", url="https://x.test")),
        ]
        for name, call in cases:
            with self.subTest(name), self.assertRaises(GateError) as caught:
                call()
            self.assertEqual(caught.exception.category, Category.REFUSED)
        raw_cases = [
            ("wrong account", json.dumps({"op": "profile", "caller": "reader", "account": "other@example.test"})),
            ("not JSON", "{op: profile"),
            ("not an object", "[1, 2]"),
            ("args not an object", json.dumps({"op": "profile", "caller": "reader", "args": [1]})),
        ]
        for name, line in raw_cases:
            with self.subTest(name):
                response = self.raw(line.encode())
                self.assertEqual((response["ok"], response["error"]["category"]), (False, "refused"))
        self.assertEqual(self.google.seen, [])
        self.assertEqual(self.op.calls(), [])
        self.assertEqual(self.served.health()["errors"]["refused"], len(cases) + len(raw_cases))

    def test_the_reader_cannot_build_a_request_that_changes_the_mailbox(self):
        self.start(READER)
        reader_ops = set(self.dispatcher.operations_for(READER))
        every_op = set(
            self.dispatcher.operations_for(CallerConfig("all", UID, frozenset({"read", "classify", "interactive"})))
        )
        self.assertTrue(every_op - reader_ops)
        for op in sorted(every_op - reader_ops):
            with self.subTest(op), self.assertRaises(GateError) as caught:
                self.client().call(op)
            # Refused for the caller's set, not merely for missing arguments.
            self.assertEqual(
                (caught.exception.category, caught.exception.detail),
                (Category.REFUSED, "operation is unknown or not allowed for this caller"),
            )
        self.assertEqual(self.google.seen, [])

    def test_a_peer_user_without_an_entry_is_refused(self):
        self.start(CallerConfig("elsewhere", UID + 1, frozenset({"read"})))
        with self.assertRaises(GateError) as caught:
            self.client("elsewhere").call("profile")
        self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.op.calls(), [])

    def test_the_socket_is_private_and_a_stale_one_is_replaced(self):
        self.start(READER)
        self.assertEqual(self.socket_path.stat().st_mode & 0o777, 0o600)
        regular = self.tmp / "not-a-socket"
        regular.write_text("x")
        with self.assertRaises(OSError):
            GateServer(regular, 0o600, self.dispatcher)


if __name__ == "__main__":
    unittest.main()


class EntryPoints(GateTestCase):
    def test_serve_reads_its_config_answers_and_stops_on_sigterm(self):
        import signal
        import subprocess
        import sys
        import time

        socket_path = self.tmp / "gate.sock"
        config = self.tmp / "config.toml"
        config.write_text(
            f'account = "{ACCOUNT}"\n'
            f'state_dir = "{self.tmp / "state"}"\n'
            f'socket = "{socket_path}"\n'
            "[vault]\n"
            'item = "synthetic-item"\nvault = "synthetic-vault"\n'
            f'op = "{self.op.directory / "op"}"\n'
            "[google]\n"
            f'gmail_base = "{self.google.base}"\ntoken_url = "{self.google.base}/token"\n'
            "[[callers]]\n"
            f'name = "reader"\nuid = {UID}\nroles = ["read"]\n'
        )
        self.google.gmail = lambda seen: Reply(200, {"emailAddress": ACCOUNT, "historyId": "3"})
        process = subprocess.Popen(
            [sys.executable, "-m", "hermes_gmail.cli", "serve", "--config", str(config)],
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        for _ in range(200):
            if socket_path.exists():
                break
            time.sleep(0.02)
        output = subprocess.run(
            [sys.executable, "-m", "hermes_gmail.cli", "profile", "--socket", str(socket_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual((output.returncode, json.loads(output.stdout)["history_id"]), (0, "3"))
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(socket_path.exists())
        self.assert_no_secret(process.stderr.read().decode(), output.stdout, output.stderr)
        process.stderr.close()
