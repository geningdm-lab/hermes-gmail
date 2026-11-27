"""Acceptance row 6: classify_apply keeps today's rules and reports each message's outcome.

Cases carried over from the classifier helper's ApplyTests (lumi@9e55b53:
automations/gmail_classifier/test_gmail_io.py), now through the real transport.
"""

import unittest

from hermes_gmail.errors import Category, GateError
from tests.harness import GateTestCase, Mailbox, Reply

LABELS = [
    {"id": "A", "name": "Area/Test", "type": "user"},
    {"id": "B", "name": "Area/Other", "type": "user"},
    {"id": "X", "name": "Action/Reply", "type": "user"},
    {"id": "INBOX", "name": "INBOX", "type": "system"},
]


def archive(message_id, *labels):
    return {"id": message_id, "add": list(labels) or ["Area/Test"], "remove": ["INBOX"]}


class Classify(GateTestCase):
    def mailbox(self, states):
        box = Mailbox(LABELS, states)
        self.google.gmail = box
        return box

    def test_batches_verify_every_message_and_keep_unread(self):
        # 1001 messages: two batches, every written message read back, UNREAD and other
        # labels untouched.
        states = {f"m{i}": {"INBOX", "UNREAD", "X"} for i in range(1001)}
        box = self.mailbox(states)
        gate = self.gate()
        result = gate.classifier.apply([archive(m) for m in states], "rules-7")
        self.assertEqual([len(w["ids"]) for w in box.writes], [1000, 1])
        self.assertEqual(len(result["verified"]), 1001)
        self.assertEqual(
            [result[k] for k in ("mismatched", "unverified", "not_attempted", "stopped")], [[], [], [], None]
        )
        self.assertTrue(all(s == {"A", "UNREAD", "X"} for s in states.values()))
        reads = [s for s in self.google.gmail_requests() if s.path.startswith("messages/m")]
        self.assertEqual(len(reads), 2002)
        self.assertTrue(all(s.query["format"] == ["minimal"] for s in reads))
        (entry,) = gate.journal().entries("classify_apply")
        self.assertEqual(
            (entry["status"], entry["rule_version"], entry["counts"]["verified"]), ("complete", "rules-7", 1001)
        )

    def test_every_preflight_failure_prevents_any_write(self):
        cases = [
            ("unknown label", {"INBOX"}, ["Missing"], []),
            ("spam", {"SPAM"}, ["Area/Test"], []),
            ("trash", {"TRASH"}, ["Area/Test"], []),
            ("sent", {"SENT"}, ["Area/Test"], []),
            ("draft", {"DRAFT"}, ["Area/Test"], []),
            ("unread removal", {"UNREAD"}, ["Area/Test"], ["UNREAD"]),
            ("second exclusive label", {"B"}, ["Area/Test"], []),
            ("no exclusive label", {"INBOX"}, [], []),
            ("system label added", {"INBOX"}, ["INBOX", "Area/Test"], []),
        ]
        for name, state, add, remove in cases:
            with self.subTest(name):
                box = self.mailbox({"a": {"INBOX"}, "b": state})
                with self.assertRaises(GateError) as caught:
                    self.gate().classifier.apply(
                        [{"id": "a", "add": ["Area/Test"], "remove": []}, {"id": "b", "add": add, "remove": remove}],
                        "rules-7",
                    )
                self.assertEqual(caught.exception.category, Category.REFUSED)
                self.assertEqual(box.writes, [])

    def test_a_race_is_reported_as_mismatched_without_restoration(self):
        box = self.mailbox({"a": {"INBOX", "UNREAD"}, "b": {"INBOX"}})
        box.after_write = lambda states: states["a"].discard("UNREAD")  # the owner read it meanwhile
        gate = self.gate()
        result = gate.classifier.apply([archive("a"), archive("b")], "rules-7")
        self.assertEqual((result["verified"], result["mismatched"]), (["b"], ["a"]))
        self.assertEqual(len(box.writes), 1)
        self.assertEqual(gate.journal().entries()[0]["status"], "partial")

    def test_a_failed_write_is_read_back_and_stops_later_batches(self):
        box = self.mailbox({"a": {"INBOX"}, "b": {"INBOX"}})
        box.on_write = lambda body: Reply(503)
        result = self.gate().classifier.apply([archive("a"), archive("b", "Area/Test", "Action/Reply")], "rules-7")
        # Two label deltas, so two batches. Gmail failed the first and changed nothing:
        # read back, it is mismatched, and the second batch is never sent.
        self.assertEqual(result["stopped"], "uncertain")
        self.assertEqual(len(box.writes), 1)
        self.assertEqual((result["mismatched"], result["not_attempted"], result["verified"]), (["a"], ["b"], []))

    def test_noop_missing_and_duplicate(self):
        box = self.mailbox({"a": {"A", "UNREAD"}})
        result = self.gate().classifier.apply([{"id": "a", "add": ["Area/Test"], "remove": []}, archive("gone")], "r")
        self.assertEqual((result["unchanged"], result["missing"], box.writes), (["a"], ["gone"], []))
        with self.assertRaises(GateError):
            self.gate().classifier.apply([archive("a"), archive("a")], "r")

    def test_plan_shape_and_rule_version_are_checked_before_any_call(self):
        gate = self.gate()
        for plan, version in (
            ({"id": "a"}, "r"),
            ([{"id": "a", "add": [], "remove": ["UNREAD"]}], "r"),
            ([{"id": "a", "add": [], "remove": [], "extra": 1}], "r"),
            ([archive("a")], ""),
            ([archive("a")], "has space"),
        ):
            with self.subTest(plan=plan, version=version), self.assertRaises(GateError) as caught:
                gate.classifier.apply(plan, version)
            self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.google.seen, [])


if __name__ == "__main__":
    unittest.main()
