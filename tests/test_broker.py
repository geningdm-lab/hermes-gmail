"""Acceptance rows 1-3: one vault read and one refresh, a category per failure, cooldown."""

import threading
import unittest

from hermes_gmail.errors import Category, GateError
from tests.harness import DOCUMENT, GateTestCase, Reply


def profile(seen):
    return Reply(200, {"emailAddress": "owner@example.test", "historyId": "1"})


class ColdStart(GateTestCase):
    def test_concurrent_callers_cause_one_vault_read_and_one_refresh(self):
        # Row 1. Breaks if the broker drops its lock around the refresh.
        self.google.gmail = profile
        gate = self.gate()
        results, errors = [], []

        def call():
            try:
                results.append(gate.transport.read("profile", deadline=gate.deadline(10)))
            except GateError as error:
                errors.append(error)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(self.op.calls()), 1)
        self.assertEqual(len(self.google.requests("token")), 1)
        self.assertEqual({s.authorization for s in self.google.gmail_requests()}, {"Bearer access-1"})

    def test_token_is_reused_until_its_last_minute(self):
        self.google.gmail = profile
        gate = self.gate()
        gate.transport.read("profile", deadline=gate.deadline(10))
        self.clock.advance(3600 - 61)
        gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual(len(self.google.requests("token")), 1)
        self.clock.advance(2)  # inside the 60-second margin
        gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual(len(self.google.requests("token")), 2)
        self.assertEqual(len(self.op.calls()), 1)


class Categories(GateTestCase):
    def failure(self, gate):
        with self.assertRaises(GateError) as caught:
            gate.transport.read("profile", deadline=gate.deadline(10))
        error = caught.exception
        self.assert_no_secret(str(error), error.to_dict(), gate.health())
        return error.category

    def test_each_failure_ends_in_its_own_category(self):
        # Row 2. A vault throttle, a revoked token, a bad client and a quota error used to
        # look alike to the caller; each must name its own fix.
        provider_body = {"error": "invalid_request", "error_description": "PROVIDER-BODY-MARKER"}
        cases = [
            ("vault throttled", "throttle", None, None, Category.VAULT_UNAVAILABLE),
            ("vault document not JSON", "ok", None, None, Category.VAULT_UNAVAILABLE),
            (
                "refresh token revoked",
                "ok",
                Reply(400, {"error": "invalid_grant", "error_description": "PROVIDER-BODY-MARKER"}),
                None,
                Category.OAUTH_INVALID_GRANT,
            ),
            ("client deleted", "ok", Reply(401, {"error": "invalid_client"}), None, Category.OAUTH_INVALID_CLIENT),
            ("token endpoint error", "ok", Reply(400, provider_body), None, Category.OAUTH_FAILED),
            ("token without access token", "ok", Reply(200, {"expires_in": 3600}), None, Category.OAUTH_FAILED),
            (
                "token with bool expiry",
                "ok",
                Reply(200, {"access_token": "a", "expires_in": True}),
                None,
                Category.OAUTH_FAILED,
            ),
            ("quota", "ok", None, Reply(429, {"error": {"message": "PROVIDER-BODY-MARKER"}}), Category.GMAIL_QUOTA),
            (
                "rate-limit 403",
                "ok",
                None,
                Reply(403, {"error": {"errors": [{"reason": "rateLimitExceeded", "message": "PROVIDER-BODY-MARKER"}]}}),
                Category.GMAIL_QUOTA,
            ),
            (
                "missing scope",
                "ok",
                None,
                Reply(
                    403, {"error": {"errors": [{"reason": "insufficientPermissions"}], "status": "PERMISSION_DENIED"}}
                ),
                Category.GMAIL_SCOPE,
            ),
            (
                "other 403",
                "ok",
                None,
                Reply(403, {"error": {"errors": [{"reason": "forbidden"}]}}),
                Category.GMAIL_FAILED,
            ),
            ("not found", "ok", None, Reply(404, {"error": {"code": 404}}), Category.GMAIL_NOT_FOUND),
            (
                "bad request",
                "ok",
                None,
                Reply(400, {"error": {"message": "PROVIDER-BODY-MARKER"}}),
                Category.GMAIL_FAILED,
            ),
            (
                "redirect",
                "ok",
                None,
                Reply(302, {}, {"Location": "http://127.0.0.1:9/elsewhere"}),
                Category.GMAIL_FAILED,
            ),
        ]
        for name, op_mode, token_reply, gmail_reply, expected in cases:
            with self.subTest(name):
                self.google.seen.clear()
                self.google.token_replies = [token_reply] if token_reply else []
                self.google.gmail = (lambda reply: lambda seen: reply)(gmail_reply or Reply(200, {}))
                self.op.set(op_mode, document="not json VAULT-OUTPUT-MARKER" if "not JSON" in name else None)
                (self.tmp / "state" / "vault-cooldown-until").unlink(missing_ok=True)
                gate = self.gate(attempts=1)
                self.assertEqual(self.failure(gate), expected)

    def test_vault_timeout_is_unavailable_not_a_hang(self):
        self.op.set("hang")
        gate = self.gate(attempts=1, timeout_seconds=0.5)
        self.assertEqual(self.failure(gate), Category.VAULT_UNAVAILABLE)

    def test_document_with_a_foreign_token_uri_is_refused(self):
        # A document pointing the refresh at another host would hand it the refresh token.
        self.op.set("ok", document={**DOCUMENT, "token_uri": "https://token.example.test/"})
        gate = self.gate(attempts=1)
        self.assertEqual(self.failure(gate), Category.VAULT_UNAVAILABLE)
        self.assertEqual(self.google.requests("token"), [])


class VaultCooldown(GateTestCase):
    def test_persistent_outage_stops_reads_and_survives_a_restart(self):
        # Row 3. A restart loop must not turn into a vault-read storm.
        self.op.set("throttle")
        gate = self.gate(attempts=3, cooldown_seconds=900)
        for _ in range(2):
            with self.assertRaises(GateError) as caught:
                gate.transport.read("profile", deadline=gate.deadline(10))
            self.assertEqual(caught.exception.category, Category.VAULT_UNAVAILABLE)
        self.assertEqual(len(self.op.calls()), 3)  # bounded attempts once, then nothing
        self.assertEqual(self.clock.sleeps[:2], [1.0, 2.0])

        restarted = self.gate(attempts=3, cooldown_seconds=900)
        with self.assertRaises(GateError):
            restarted.transport.read("profile", deadline=restarted.deadline(10))
        self.assertEqual(len(self.op.calls()), 3)
        self.assertIsNotNone(restarted.health()["vault_cooldown_until"])

        self.op.set("ok")
        self.google.gmail = profile
        self.clock.advance(901)
        restarted.transport.read("profile", deadline=restarted.deadline(10))
        self.assertEqual(len(self.op.calls()), 4)
        self.assertIsNone(restarted.health()["vault_cooldown_until"])

    def test_one_failed_read_then_success_needs_no_cooldown(self):
        self.op.set("throttle", "ok")
        self.google.gmail = profile
        gate = self.gate(attempts=3)
        gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual(len(self.op.calls()), 2)
        self.assertIsNone(gate.health()["vault_cooldown_until"])


class RejectionHold(GateTestCase):
    def test_rejected_credential_is_not_retried_until_the_hold_ends(self):
        # After invalid_grant every call would otherwise hit Google and 1Password again.
        self.google.gmail = profile
        self.google.token_replies = [Reply(400, {"error": "invalid_grant"})]
        gate = self.gate(cooldown_seconds=900)
        for _ in range(3):
            with self.assertRaises(GateError) as caught:
                gate.transport.read("profile", deadline=gate.deadline(10))
            self.assertEqual(caught.exception.category, Category.OAUTH_INVALID_GRANT)
        self.assertEqual(len(self.google.requests("token")), 1)
        self.assertEqual(len(self.op.calls()), 1)

        # The owner reauthorized and updated the document; after the hold it is read again.
        self.clock.advance(901)
        gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual(len(self.op.calls()), 2)
        self.assertEqual(len(self.google.requests("token")), 2)

    def test_forced_refresh_reports_the_category_in_health(self):
        self.google.token_replies = [Reply(200, {"access_token": "a"}), Reply(400, {"error": "invalid_grant"})]
        gate = self.gate()
        self.assertEqual(gate.health(force_refresh=True)["forced_refresh"], "ok")
        snapshot = gate.health(force_refresh=True)
        self.assertEqual(snapshot["forced_refresh"], "oauth_invalid_grant")
        self.assertEqual(snapshot["errors"]["oauth_invalid_grant"], 1)
        self.assert_no_secret(snapshot)


if __name__ == "__main__":
    unittest.main()
