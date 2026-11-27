"""Acceptance row 13: a draft is sent at most once, whatever happens to the answer."""

import unittest

from hermes_gmail.errors import Category, GateError
from hermes_gmail.gate import Gate
from tests.harness import DROP, Reply, _payload
from tests.test_interactive import InteractiveTestCase


class Send(InteractiveTestCase):
    def setUp(self):
        super().setUp()
        self.box.threads["t1"] = [
            {
                "id": "m1",
                "threadId": "t1",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Plans"},
                        {"name": "Message-ID", "value": "<m1@example.test>"},
                    ]
                },
            }
        ]
        self.draft = self.ui.draft_put(["friend@example.test"], "Re: Plans", "See you at noon.", thread_id="t1")[
            "draft_id"
        ]

    def sends(self):
        return [w for w in self.box.writes if isinstance(w, tuple) and w[0] == "draft_send"]

    def prepare(self, gate=None):
        return (gate or self.gate_).sender.send_prepare(self.draft)

    def apply(self, preview, gate=None):
        return (gate or self.gate_).sender.send_apply(preview["preview_id"], "elicitation")

    def status(self):
        return [e["status"] for e in self.gate_.journal().entries("send_apply")]

    def test_an_approved_send_happens_once_and_is_read_back(self):
        preview = self.prepare()
        self.assertEqual(preview["summary"], 'Send the draft "Re: Plans": to friend@example.test.')
        result = self.apply(preview)
        self.assertEqual((result["status"], result["verified"]), ("sent", True))
        self.assertNotIn(self.draft, self.box.drafts)
        self.assertEqual(self.status(), ["sent"])
        with self.assertRaises(GateError):
            self.apply(preview)  # the same approval again
        self.assertEqual(len(self.sends()), 1)

    def lost_answer(self, reply_status):
        def sent_then_lost(seen):
            if seen.path == "drafts/send":
                with self.box.lock:
                    self.box._serve(seen)  # Gmail sends it...
                return Reply(reply_status)  # ...and the answer never arrives intact
            return None

        self.box.intercept = sent_then_lost
        spare = self.prepare()  # a second approval prepared before the first send
        with self.assertRaises(GateError) as caught:
            self.apply(self.prepare())
        self.assertEqual(caught.exception.category, Category.UNCERTAIN)
        self.assertEqual(self.status()[-1], "uncertain")
        for again in (self.prepare, lambda: self.apply(spare)):
            with self.assertRaises(GateError) as refused:
                again()
            self.assertEqual(refused.exception.category, Category.REFUSED)
        self.box.intercept = lambda seen: None
        self.assertEqual(self.gate_.sender.send_reconcile(self.draft)["status"], "sent")
        self.assertEqual(self.status()[-1], "sent")
        self.assertEqual(len(self.sends()), 1)

    def test_a_5xx_after_acceptance_is_uncertain_until_reconciled(self):
        self.lost_answer(503)

    def test_a_connection_dropped_after_acceptance_is_uncertain_until_reconciled(self):
        self.lost_answer(DROP)

    def test_a_send_that_did_not_happen_reconciles_as_not_sent_and_may_be_retried(self):
        self.box.intercept = lambda seen: Reply(503) if seen.path == "drafts/send" else None
        spare = self.prepare()
        with self.assertRaises(GateError):
            self.apply(self.prepare())
        self.box.intercept = lambda seen: None
        # The draft is still there, but until reconcile says so nothing may send it.
        with self.assertRaises(GateError) as refused:
            self.apply(spare)
        self.assertEqual(refused.exception.category, Category.REFUSED)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.gate_.sender.send_reconcile(self.draft), {"status": "not_sent"})
        self.assertEqual(self.apply(self.prepare())["status"], "sent")
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.status(), ["not_sent", "sent"])

    def test_a_restart_with_a_send_in_flight_refuses_until_reconciled(self):
        preview = self.prepare()
        # The process recorded the send and died before Gmail's answer arrived; Gmail sent it.
        stored = self.gate_.journal().get_preview(preview["preview_id"])
        self.gate_.journal().record(
            caller="local",
            operation="send_apply",
            status="in_flight",
            preview_hash=stored.hash,
            approval="elicitation",
            gmail_ids=[self.draft, "t1"],
        )
        with self.box.lock:
            self.box._serve(type("S", (), {"method": "POST", "path": "drafts/send", "body": {"id": self.draft}})())
        self.gate_.close()
        restarted = Gate(self.config(), wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)
        self.addCleanup(restarted.close)
        with self.assertRaises(GateError) as caught:
            restarted.sender.send_prepare(self.draft)
        self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(restarted.sender.send_reconcile(self.draft)["status"], "sent")
        self.assertEqual(len(self.sends()), 1)

    def test_a_draft_changed_since_the_preview_is_not_sent(self):
        preview = self.prepare()
        self.ui.draft_put(["friend@example.test", "other@example.test"], "Re: Plans", "Changed.", thread_id="t1")
        with self.assertRaises(GateError) as caught:
            self.apply(preview)
        self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.sends(), [])

    def test_a_gone_draft_without_a_new_sent_message_stays_uncertain(self):
        # An identical message the owner sent earlier in the thread is not this send.
        with self.box.lock:
            earlier = dict(self.box.drafts[self.draft])
        self.box.threads["t1"].append(
            {"id": "s0", "threadId": "t1", "labelIds": ["SENT"], "payload": _payload(earlier["raw"])}
        )
        self.box.intercept = lambda seen: Reply(503) if seen.path == "drafts/send" else None
        with self.assertRaises(GateError):
            self.apply(self.prepare())
        self.box.intercept = lambda seen: None
        self.box.drafts.pop(self.draft)  # the owner deleted the draft by hand
        self.assertEqual(self.gate_.sender.send_reconcile(self.draft), {"status": "uncertain"})
        self.assertEqual(self.status(), ["uncertain"])

    def test_reconcile_with_nothing_open_is_refused(self):
        with self.assertRaises(GateError) as caught:
            self.gate_.sender.send_reconcile(self.draft)
        self.assertEqual(caught.exception.category, Category.REFUSED)


if __name__ == "__main__":
    unittest.main()
