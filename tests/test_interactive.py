"""Acceptance rows 9-12: previews that stop on any change, approval by elicitation, drafts and
filters with read-back."""

import base64
import unittest

import anyio
from mcp import Client
from mcp_types import ElicitResult

from hermes_gmail.client import GateClient
from hermes_gmail.errors import Category, GateError
from hermes_gmail.mcp_adapter import build_server
from tests.harness import GateTestCase, Mailbox, Reply
from tests.test_service import AGENT, ServiceTestCase

LABELS = [
    {"id": "Label_A", "name": "Area/Test", "type": "user"},
    {"id": "Label_B", "name": "Projects", "type": "user"},
    {"id": "INBOX", "name": "INBOX", "type": "system"},
]


class InteractiveTestCase(GateTestCase):
    def setUp(self):
        super().setUp()
        self.box = Mailbox([dict(label) for label in LABELS], {"a": {"INBOX", "UNREAD"}, "b": {"INBOX"}})
        self.google.gmail = self.box
        self.gate_ = self.gate()
        self.ui = self.gate_.interactive

    def batch_writes(self):
        return [w for w in self.box.writes if isinstance(w, dict)]


class Modify(InteractiveTestCase):
    def prepare(self, **kwargs):
        args = {"ids": ["a", "b"], "add": ["Area/Test"], "remove": ["INBOX"], **kwargs}
        return self.ui.modify_prepare(**args)["preview_id"]

    def test_an_approved_preview_is_applied_and_read_back(self):
        preview = self.prepare()
        result = self.ui.modify_apply(preview, "elicitation")
        self.assertEqual((sorted(result["verified"]), result["mismatched"]), (["a", "b"], []))
        self.assertEqual(self.box.states, {"a": {"Label_A", "UNREAD"}, "b": {"Label_A"}})
        (entry,) = self.gate_.journal().entries("modify_apply")
        self.assertEqual((entry["status"], entry["approval"]), ("done", "elicitation"))

    def test_apply_stops_before_writing_when_anything_changed(self):
        # Row 9: an owner action since the preview, an expired preview and a reused one.
        def owner_starred_a():
            self.box.states["a"].add("STARRED")

        cases = [
            ("owner action since the preview", owner_starred_a),
            ("message deleted since the preview", lambda: self.box.states.pop("b")),
            ("preview expired", lambda: self.clock.advance(901)),
        ]
        for name, change in cases:
            with self.subTest(name):
                self.box.states = {"a": {"INBOX", "UNREAD"}, "b": {"INBOX"}}
                self.box.writes.clear()
                preview = self.prepare()
                change()
                with self.assertRaises(GateError) as caught:
                    self.ui.modify_apply(preview, "elicitation")
                self.assertEqual(caught.exception.category, Category.REFUSED)
                self.assertEqual(self.batch_writes(), [])

    def test_a_preview_is_applied_once_only_with_approval_and_only_by_its_caller(self):
        preview = self.prepare()
        for approval, caller in ((None, "local"), ("yes", "local"), ("elicitation", "someone-else")):
            with self.subTest(approval=approval, caller=caller), self.assertRaises(GateError) as caught:
                self.ui.modify_apply(preview, approval, caller=caller)
            self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.batch_writes(), [])
        self.ui.modify_apply(preview, "elicitation")
        with self.assertRaises(GateError):
            self.ui.modify_apply(preview, "elicitation")
        self.assertEqual(len(self.batch_writes()), 1)

    def test_two_concurrent_applies_of_one_preview_write_once(self):
        import contextlib
        import threading

        preview = self.prepare(ids=["a"])
        # Hold each apply's state read until both have read, so a second apply that got
        # past the preview would also pass the state check. Correct code refuses it first;
        # then the lone reader waits out the barrier and goes on.
        barrier = threading.Barrier(2, timeout=1)

        def hold_reads(seen):
            if seen.path == "messages/a" and seen.method == "GET":
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return None

        self.box.intercept = hold_reads
        outcomes = []

        def apply():
            try:
                outcomes.append(self.ui.modify_apply(preview, "elicitation"))
            except GateError as error:
                outcomes.append(error.category)

        threads = [threading.Thread(target=apply) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.batch_writes()), 1)
        self.assertIn(Category.REFUSED, outcomes)

    def test_a_race_after_the_write_is_reported_not_hidden(self):
        preview = self.prepare()
        self.box.after_write = lambda states: states["a"].add("INBOX")
        result = self.ui.modify_apply(preview, "elicitation")
        self.assertEqual((result["verified"], result["mismatched"]), (["b"], ["a"]))

    def test_a_refused_write_leaves_targets_retryable(self):
        preview = self.prepare()
        self.box.on_write = lambda body: Reply(429)
        result = self.ui.modify_apply(preview, "elicitation")
        self.assertEqual(
            (result["stopped"], sorted(result["not_attempted"]), result["mismatched"]), ("gmail_quota", ["a", "b"], [])
        )

    def test_the_summary_leads_with_the_action_and_quotes_names(self):
        self.box.labels.append({"id": "Label_X", "name": 'Fake" remove INBOX\nok', "type": "user"})
        summary = self.ui.modify_prepare(ids=["a"], add=['Fake" remove INBOX\nok'])["summary"]
        # The name stays one quoted item: its quote is escaped and its line break folded.
        self.assertEqual(summary, 'Change labels on 1 message(s): add "Fake\\" remove INBOX ok".')

    def test_prepare_refuses_what_it_must_not_touch(self):
        self.box.states["t"] = {"TRASH"}
        self.box.search_ids = []
        cases = [
            ("trash target", {"ids": ["t"]}),
            ("unknown label", {"add": ["Nope"]}),
            ("spam is not a modifiable label", {"add": ["SPAM"]}),
            ("ids and query together", {"query": "in:inbox"}),
            ("query with no match", {"ids": None, "query": "in:inbox"}),
            ("nothing to change", {"add": [], "remove": []}),
        ]
        for name, kwargs in cases:
            with self.subTest(name), self.assertRaises(GateError) as caught:
                self.prepare(**kwargs)
            self.assertEqual(caught.exception.category, Category.REFUSED)

    def test_query_targets_and_summary(self):
        self.box.search_ids = ["a", "b"]
        prepared = self.ui.modify_prepare(query="in:inbox", remove=["UNREAD"])
        self.assertEqual(prepared["summary"], 'Change labels on 2 message(s): remove "UNREAD".')
        self.assertEqual(prepared["ids"], ["a", "b"])
        self.assertNotIn("before", prepared)


class Labels(InteractiveTestCase):
    def test_label_create_is_idempotent_by_name(self):
        self.assertEqual(self.ui.label_create("Area/Test"), {"label": LABELS[0], "created": False})
        created = self.ui.label_create("Area/New")
        self.assertTrue(created["created"])
        self.assertEqual(
            [w for w in self.box.writes if w[0] == "labels"],
            [("labels", {"name": "Area/New", "labelListVisibility": "labelShow", "messageListVisibility": "show"})],
        )
        for name in ("INBOX", " padded", "", "x" * 226, "two\nlines"):
            with self.subTest(name), self.assertRaises(GateError):
                self.ui.label_create(name)


class Filters(InteractiveTestCase):
    def test_create_reads_back_with_label_arrays_as_sets(self):
        # Row 10 of the design's filters: Gmail may return label arrays in another order.
        def reorder(seen):
            if seen.path == "settings/filters" and seen.method == "GET" and self.box.filters:
                listed = [
                    {**f, "action": {k: list(reversed(v)) for k, v in f["action"].items()}} for f in self.box.filters
                ]
                return Reply(200, {"filter": listed})
            return None

        self.box.intercept = reorder
        preview = self.ui.filter_prepare({"from": "news@example.test"}, add=["Projects", "STARRED"], remove=["INBOX"])
        result = self.ui.filter_apply(preview["preview_id"], "elicitation")
        self.assertTrue(result["verified"])
        self.assertEqual(self.box.filters[0]["action"]["addLabelIds"], ["Label_B", "STARRED"])

    def test_more_than_one_user_label_or_a_duplicate_is_refused(self):
        with self.assertRaises(GateError):
            self.ui.filter_prepare({"from": "x@example.test"}, add=["Projects", "Area/Test"])
        preview = self.ui.filter_prepare({"from": "x@example.test"}, add=["Projects"])
        self.ui.filter_apply(preview["preview_id"], "elicitation")
        with self.assertRaises(GateError) as caught:
            self.ui.filter_prepare({"from": "x@example.test"}, add=["Projects"])
        self.assertEqual(caught.exception.detail, "an identical filter already exists")
        for criteria in ({}, {"forward": "x@example.test"}, {"from": ""}, {"hasAttachment": "yes"}):
            with self.subTest(criteria), self.assertRaises(GateError):
                self.ui.filter_prepare(criteria, add=["Projects"])

    def test_a_create_whose_answer_was_lost_is_reconciled_by_reading(self):
        def created_but_lost(seen):
            if seen.path == "settings/filters" and seen.method == "POST":
                with self.box.lock:
                    self.box._serve(seen)
                return Reply(503)
            return None

        self.box.intercept = created_but_lost
        preview = self.ui.filter_prepare({"subject": "invoice"}, add=["Projects"])
        self.assertTrue(self.ui.filter_apply(preview["preview_id"], "elicitation")["verified"])
        self.assertEqual(self.gate_.journal().entries("filter_apply")[0]["status"], "done")

    def test_a_create_that_did_not_happen_stays_uncertain(self):
        self.box.intercept = lambda seen: Reply(503) if seen.method == "POST" else None
        preview = self.ui.filter_prepare({"subject": "invoice"}, add=["Projects"])
        with self.assertRaises(GateError) as caught:
            self.ui.filter_apply(preview["preview_id"], "elicitation")
        self.assertEqual(caught.exception.category, Category.UNCERTAIN)
        self.assertEqual(self.gate_.journal().entries("filter_apply")[0]["status"], "uncertain")

    def test_a_created_filter_whose_read_back_fails_is_reported_unverified(self):
        preview = self.ui.filter_prepare({"from": "x@example.test"}, add=["Projects"])
        created = []

        def fail_reads_after_create(seen):
            if seen.path == "settings/filters" and seen.method == "POST":
                created.append(True)
            elif seen.path == "settings/filters" and created:
                return Reply(400)
            return None

        self.box.intercept = fail_reads_after_create
        result = self.ui.filter_apply(preview["preview_id"], "elicitation")
        self.assertEqual((result["verified"], result["filter"]["id"]), (False, self.box.filters[0]["id"]))
        self.assertEqual(self.gate_.journal().entries("filter_apply")[0]["status"], "unverified")
        self.assertEqual(self.ui.preview(preview["preview_id"])["status"], "used")

    def test_criteria_come_after_the_action_and_are_bounded(self):
        summary = self.ui.filter_prepare({"query": "x" * 500}, remove=["INBOX"])["summary"]
        self.assertTrue(summary.startswith('Create a filter that will remove "INBOX", for mail matching query "xxx'))
        self.assertLess(len(summary), 200)

    def test_delete_checks_the_filter_is_unchanged_and_reads_back(self):
        self.box.filters = [
            {"id": "F9", "criteria": {"from": "old@example.test"}, "action": {"addLabelIds": ["Label_B"]}}
        ]
        preview = self.ui.filter_delete_prepare("F9")
        self.assertEqual(
            preview["summary"],
            'Delete the filter that would add "Projects", for mail matching from "old@example.test".',
        )
        self.assertTrue(self.ui.filter_delete_apply(preview["preview_id"], "elicitation")["verified"])
        self.assertEqual(self.box.filters, [])

        self.box.filters = [
            {"id": "F8", "criteria": {"from": "a@example.test"}, "action": {"addLabelIds": ["Label_B"]}}
        ]
        stale = self.ui.filter_delete_prepare("F8")
        self.box.filters[0]["action"] = {"addLabelIds": ["Label_A"]}
        with self.assertRaises(GateError) as caught:
            self.ui.filter_delete_apply(stale["preview_id"], "elicitation")
        self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual([w for w in self.box.writes if w[0] == "filter_delete"], [("filter_delete", "F9")])


class Drafts(InteractiveTestCase):
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
                        {"name": "References", "value": "<m0@example.test>"},
                    ]
                },
            }
        ]

    def put(self, body="See you at noon.", **kwargs):
        args = {"to": ["Friend <friend@example.test>"], "subject": "Re: Plans", "body": body, "thread_id": "t1"}
        return self.ui.draft_put(**{**args, **kwargs})

    def test_one_draft_per_thread_with_threading_headers_and_semantic_read_back(self):
        first = self.put()
        second = self.put(body="See you at one.")
        self.assertEqual((first["created"], second["created"]), (True, False))
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertTrue(first["verified"] and second["verified"])
        self.assertEqual(len(self.box.drafts), 1)
        # The thread now lists the first draft too; the reply still targets m1, not the draft.
        headers = {h["name"]: h["value"] for h in _stored(self.box, first["draft_id"])["headers"]}
        self.assertEqual(headers["In-Reply-To"], "<m1@example.test>")
        self.assertEqual(headers["References"], "<m0@example.test> <m1@example.test>")

    def test_an_update_by_draft_id_alone_stays_in_its_thread(self):
        first = self.put()
        second = self.ui.draft_put(["friend@example.test"], "Re: Plans", "Changed.", draft_id=first["draft_id"])
        self.assertTrue(second["verified"])
        self.assertEqual(self.box.drafts[first["draft_id"]]["thread_id"], "t1")
        update = [w for w in self.box.writes if isinstance(w, tuple) and w[0] == "draft_update"][-1][1]
        self.assertEqual(update["message"]["threadId"], "t1")
        headers = {h["name"]: h["value"] for h in _stored(self.box, first["draft_id"])["headers"]}
        self.assertEqual(headers["In-Reply-To"], "<m1@example.test>")

    def test_a_stored_draft_that_differs_is_not_verified(self):
        def drop(name):
            return lambda payload: payload.__setitem__("headers", [h for h in payload["headers"] if h["name"] != name])

        def other_body(payload):
            payload["body"]["data"] = base64.urlsafe_b64encode(b"Something else").decode()

        for name, alter in (
            ("recipient missing", drop("To")),
            ("threading header missing", drop("In-Reply-To")),
            ("subject missing", drop("Subject")),
            ("body differs", other_body),
        ):
            with self.subTest(name):
                self.box.drafts.clear()

                def altered(seen, alter=alter):
                    if seen.path.startswith("drafts/") and seen.method == "GET":
                        with self.box.lock:
                            reply = self.box._serve(seen)
                        alter(reply.body["message"]["payload"])
                        return reply
                    return None

                self.box.intercept = altered
                self.assertFalse(self.put()["verified"])
        statuses = {e["status"] for e in self.gate_.journal().entries("draft_put")}
        self.assertEqual(statuses, {"mismatched"})

    def test_refusals(self):
        self.put()
        self.box.drafts["r99"] = {"message_id": "m99", "thread_id": "t1", "raw": ""}
        cases = [
            ("several drafts in the thread", {}),
            ("header injection in an address", {"to": ["a@example.test\nBcc: b@example.test"], "draft_id": "r99"}),
            ("header injection in the subject", {"subject": "Hi\nBcc: x@example.test", "draft_id": "r99"}),
            ("draft from another thread", {"draft_id": "r1000"}),
            ("no recipients", {"to": [], "draft_id": "r99"}),
        ]
        for name, kwargs in cases:
            with self.subTest(name), self.assertRaises(GateError) as caught:
                self.put(**kwargs)
            self.assertEqual(caught.exception.category, Category.REFUSED)

    def test_a_lost_create_is_uncertain_and_journaled(self):
        self.box.intercept = lambda seen: Reply(503) if seen.path == "drafts" and seen.method == "POST" else None
        with self.assertRaises(GateError) as caught:
            self.put()
        self.assertEqual(caught.exception.category, Category.UNCERTAIN)
        self.assertEqual(self.gate_.journal().entries("draft_put")[0]["status"], "uncertain")


def _stored(box, draft_id):
    from tests.harness import _payload

    return _payload(box.drafts[draft_id]["raw"])


class Elicitation(ServiceTestCase):
    """Row 10: an apply asks the owner with the gate's own summary; anything but yes writes nothing."""

    def setUp(self):
        super().setUp()
        self.box = Mailbox([dict(label) for label in LABELS], {"a": {"INBOX"}})
        self.google.gmail = self.box
        self.start(AGENT)
        self.agent = GateClient(self.socket_path, caller="agent", timeout=10)

    mode = "auto"  # the SDK's default: the 2026-07-28 protocol, answers ride the retry

    def apply_via_adapter(self, answer):
        asked = []

        async def callback(context, params):
            asked.append(params.message)
            return ElicitResult(action=answer, content={} if answer == "accept" else None)

        preview = self.agent.call("modify_prepare", ids=["a"], remove=["INBOX"])["preview_id"]

        async def run():
            # "unsupported": a client that declares no elicitation capability at all.
            options = {"mode": self.mode} | ({} if answer == "unsupported" else {"elicitation_callback": callback})
            async with Client(build_server(self.agent), **options) as client:
                try:
                    return await client.call_tool("modify_apply", {"preview_id": preview})
                except Exception as error:  # the SDK may raise instead of returning an error result
                    return error

        return preview, asked, anyio.run(run)

    def test_accept_applies_and_the_question_is_the_stored_summary(self):
        _, asked, result = self.apply_via_adapter("accept")
        self.assertFalse(result.is_error)
        self.assertEqual(asked, ['Change labels on 1 message(s): remove "INBOX". Approve this change?'])
        self.assertEqual(self.box.states["a"], set())

    def test_decline_cancel_or_no_elicitation_writes_nothing_and_keeps_the_preview(self):
        for answer in ("decline", "cancel", "unsupported"):
            with self.subTest(answer):
                self.box.states["a"] = {"INBOX"}
                preview, _, result = self.apply_via_adapter(answer)
                self.assertTrue(result.is_error)
                self.assertIn("declined:", result.content[0].text)
                self.assertEqual(self.box.writes, [])
                self.assertEqual(self.agent.call("preview", preview_id=preview)["status"], "open")


class ElicitationLegacy(Elicitation):
    """The initialize-handshake protocol (<= 2025-11-25): the question is a request mid-call."""

    mode = "legacy"


if __name__ == "__main__":
    unittest.main()
