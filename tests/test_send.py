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
        self.assertEqual(preview["summary"], 'Send this draft: to "friend@example.test"; subject "Re: Plans".')
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
        from unittest import mock

        from hermes_gmail.journal import Journal

        class Crash(Exception):
            pass

        preview = self.prepare()
        # Gmail sends; the process dies before it can record the answer.
        with mock.patch.object(Journal, "update", side_effect=Crash), self.assertRaises(Crash):
            self.apply(preview)
        self.assertEqual(self.status(), ["in_flight"])
        self.gate_.close()
        restarted = Gate(self.config(), wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)
        self.addCleanup(restarted.close)
        with self.assertRaises(GateError) as caught:
            restarted.sender.send_prepare(self.draft)
        self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(restarted.sender.send_reconcile(self.draft)["status"], "sent")
        self.assertEqual(len(self.sends()), 1)

    def test_two_concurrent_applies_of_one_draft_send_once(self):
        import contextlib
        import threading

        previews = [self.prepare(), self.prepare()]
        barrier = threading.Barrier(2, timeout=1)

        def hold_draft_reads(seen):
            # Both applies read the draft at the same moment unless the send lock orders them.
            if seen.path == f"drafts/{self.draft}" and seen.method == "GET":
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return None

        self.box.intercept = hold_draft_reads
        outcomes = []

        def apply(preview):
            try:
                outcomes.append(self.apply(preview)["status"])
            except GateError as error:
                outcomes.append(error.category)

        threads = [threading.Thread(target=apply, args=(p,)) for p in previews]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(sorted(map(str, outcomes)), ["refused", "sent"])

    def test_reconcile_and_draft_put_wait_for_a_running_send(self):
        import threading

        release, entered = threading.Event(), threading.Event()

        def slow_send(seen):
            if seen.path == "drafts/send":
                entered.set()
                release.wait(5)
            return None

        self.box.intercept = slow_send
        sender = threading.Thread(target=self.apply, args=(self.prepare(),))
        sender.start()
        self.assertTrue(entered.wait(5))
        for name, call in (
            ("reconcile", lambda: self.gate_.sender.send_reconcile(self.draft)),
            ("draft_put", lambda: self.ui.draft_put(["x@example.test"], "Re: Plans", "Other.", draft_id=self.draft)),
        ):
            with self.subTest(name), self.assertRaises(GateError) as caught:
                call()
            self.assertEqual(caught.exception.category, Category.REFUSED)
        release.set()
        sender.join()
        self.assertEqual((len(self.sends()), self.status()), (1, ["sent"]))

    def test_a_failed_read_back_after_the_send_still_reports_sent(self):
        self.box.intercept = lambda seen: Reply(400) if seen.path.startswith("messages/s") else None
        result = self.apply(self.prepare())
        self.assertEqual((result["status"], result["verified"]), ("sent", False))
        self.assertEqual(self.status(), ["sent"])

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

    def test_a_draft_with_cc_reconciles_as_sent(self):
        self.ui.draft_put(
            ["friend@example.test"], "Re: Plans", "See you at noon.", cc=["boss@example.test"], thread_id="t1"
        )

        def sent_then_lost(seen):
            if seen.path == "drafts/send":
                with self.box.lock:
                    self.box._serve(seen)
                return Reply(503)
            return None

        self.box.intercept = sent_then_lost
        preview = self.prepare()
        self.assertIn('cc "boss@example.test"', preview["summary"])
        with self.assertRaises(GateError):
            self.apply(preview)
        self.box.intercept = lambda seen: None
        self.assertEqual(self.gate_.sender.send_reconcile(self.draft)["status"], "sent")

    def test_a_sent_message_to_other_recipients_is_not_this_send(self):
        self.box.intercept = lambda seen: Reply(503) if seen.path == "drafts/send" else None
        with self.assertRaises(GateError):
            self.apply(self.prepare())
        self.box.intercept = lambda seen: None
        # The owner deleted the draft and sent something similar by hand, with a Cc added.
        from hermes_gmail.compose import Draft

        other = Draft.parse(["friend@example.test"], ["boss@example.test"], "Re: Plans", "See you at noon.")
        self.box.drafts.pop(self.draft)
        self.box.threads["t1"].append(
            {"id": "s9", "threadId": "t1", "labelIds": ["SENT"], "payload": _payload(other.raw())}
        )
        self.assertEqual(self.gate_.sender.send_reconcile(self.draft), {"status": "uncertain"})

    def test_reconcile_with_nothing_open_is_refused(self):
        with self.assertRaises(GateError) as caught:
            self.gate_.sender.send_reconcile(self.draft)
        self.assertEqual(caught.exception.category, Category.REFUSED)


if __name__ == "__main__":
    unittest.main()
