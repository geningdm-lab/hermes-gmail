"""Acceptance row 4 (search coverage, failed reads in a batch) and the other read operations."""

import unittest

from hermes_gmail.errors import Category, GateError
from tests.harness import GateTestCase, Reply
from tests.test_mime import b64


def router(routes):
    """Gmail handler from {path: Reply or callable(seen) -> Reply}; anything else is 404."""

    def handler(seen):
        reply = routes.get(seen.path)
        if reply is None:
            return Reply(404, {"error": {"code": 404}})
        return reply(seen) if callable(reply) else reply

    return handler


def message(message_id, labels=("INBOX",), **payload):
    return {
        "id": message_id,
        "threadId": "t1",
        "labelIds": list(labels),
        "internalDate": "1700000000000",
        "snippet": "synthetic snippet",
        "payload": {"headers": [{"name": "Subject", "value": f"Subject {message_id}"}], **payload},
    }


class Search(GateTestCase):
    def pages(self, pages):
        """messages.list answering by pageToken: {None: page, "t1": page, ...}."""

        def answer(seen):
            token = seen.query.get("pageToken", [None])[0]
            return Reply(200, pages[token])

        self.google.gmail = router({"messages": answer})

    def test_coverage_is_stated_exactly(self):
        cases = [
            ("zero results", {None: {"resultSizeEstimate": 0}}, 5, ([], True)),
            (
                "several pages, duplicate across pages, estimate ignored",
                {
                    None: {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2", "resultSizeEstimate": 999},
                    "p2": {"messages": [{"id": "b"}, {"id": "c"}]},
                },
                5,
                (["a", "b", "c"], True),
            ),
            (
                "page bound reached",
                {None: {"messages": [{"id": "a"}], "nextPageToken": "p2"}, "p2": {"messages": [{"id": "b"}]}},
                1,
                (["a"], False),
            ),
        ]
        for name, pages, page_bound, (ids, complete) in cases:
            with self.subTest(name):
                self.pages(pages)
                result = self.gate().reads.search("in:inbox", page_bound)
                self.assertEqual((result["ids"], result["fetched"], result["complete"]), (ids, len(ids), complete))
                self.assertEqual("next_page_token" in result, not complete)

    def test_continuation_picks_up_at_the_token(self):
        self.pages({None: {"messages": [{"id": "a"}], "nextPageToken": "p2"}, "p2": {"messages": [{"id": "b"}]}})
        gate = self.gate()
        first = gate.reads.search("q", 1)
        second = gate.reads.search("q", 1, first["next_page_token"])
        self.assertEqual((second["ids"], second["complete"]), (["b"], True))

    def test_a_repeated_page_token_fails_instead_of_looping(self):
        self.pages({None: {"messages": [{"id": "a"}], "nextPageToken": "p2"}, "p2": {"nextPageToken": "p2"}})
        with self.assertRaises(GateError) as caught:
            self.gate().reads.search("q", 50)
        self.assertEqual(caught.exception.category, Category.GMAIL_FAILED)

    def test_out_of_bounds_requests_are_refused_before_any_call(self):
        gate = self.gate()
        for args in (("x" * 8193, 1), ("q", 0), ("q", 101), ("q", True), (None, 1)):
            with self.subTest(args), self.assertRaises(GateError) as caught:
                gate.reads.search(*args)
            self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.google.seen, [])
        self.assertEqual(self.op.calls(), [])


class ReadMessages(GateTestCase):
    def test_a_failed_read_inside_a_batch_is_reported_per_message(self):
        self.google.gmail = router(
            {
                "messages/a": Reply(200, message("a")),
                "messages/c": Reply(200, message("other")),  # a reply for another message
            }
        )
        result = self.gate().reads.read_messages(["a", "b", "c"], "metadata", ["Subject"])
        self.assertEqual([m["id"] for m in result["messages"]], ["a"])
        self.assertEqual(
            result["failed"],
            [{"id": "b", "category": "gmail_not_found"}, {"id": "c", "category": "gmail_failed"}],
        )
        self.assertEqual(result["fetched"], 1)
        sent = {s.path: s.query for s in self.google.gmail_requests()}
        self.assertEqual(sent["messages/a"]["metadataHeaders"], ["Subject"])

    def test_full_read_carries_text_and_flags(self):
        body = {"mimeType": "text/html", "body": {"data": b64("<p>Hi <img src='http://127.0.0.1:9/pixel'></p>")}}
        self.google.gmail = router({"messages/a": Reply(200, message("a", **body))})
        view = self.gate().reads.read_messages(["a"], "full")["messages"][0]
        self.assertEqual((view["text"], view["text_source"], view["decode_failed"]), ("Hi", "html", False))
        self.assertEqual([s.path for s in self.google.gmail_requests()], ["messages/a"])  # nothing remote

    def test_bounds(self):
        gate = self.gate()
        for ids in ([], [f"m{i}" for i in range(101)], ["a/../b"], ["a", "a"], "a"):
            with self.subTest(ids), self.assertRaises(GateError) as caught:
                gate.reads.read_messages(ids)
            self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.google.seen, [])


class OtherReads(GateTestCase):
    def test_thread_keeps_the_latest_messages_with_direction_and_reply_headers(self):
        labels = {2: ("SENT",), 4: ("DRAFT",)}
        messages = [message(f"m{i}", labels=labels.get(i, ("INBOX",))) for i in range(1, 5)]
        for m in messages:
            m["payload"]["headers"].append({"name": "Received", "value": "hop"})
            m["payload"]["headers"].append({"name": "Message-ID", "value": f"<{m['id']}@example.test>"})
        self.google.gmail = router({"threads/t1": Reply(200, {"id": "t1", "messages": messages})})
        result = self.gate().reads.read_thread("t1", 3)
        self.assertEqual(
            [(m["id"], m["direction"]) for m in result["messages"]],
            [("m2", "sent"), ("m3", "received"), ("m4", "draft")],
        )
        self.assertEqual((result["complete"], result["total"]), (False, 4))
        self.assertEqual([h["name"] for h in result["messages"][0]["headers"]], ["Subject", "Message-ID"])

    def test_history_aggregates_changes_and_reports_coverage(self):
        def answer(seen):
            token = seen.query.get("pageToken", [None])[0]
            if token is None:
                return Reply(
                    200,
                    {
                        "history": [
                            {"messagesAdded": [{"message": {"id": "a"}}]},
                            {"labelsAdded": [{"message": {"id": "a"}, "labelIds": ["STARRED"]}]},
                        ],
                        "historyId": "120",
                        "nextPageToken": "h2",
                    },
                )
            return Reply(200, {"history": [{"messagesAdded": [{"message": {"id": "b"}}]}], "historyId": "121"})

        self.google.gmail = router({"history": answer})
        gate = self.gate()
        partial = gate.reads.history("100", 1)
        self.assertEqual((partial["complete"], partial["next_page_token"]), (False, "h2"))
        full = gate.reads.history("100", 5)
        self.assertEqual(
            (full["messages_added"], full["labels_added"], full["history_id"], full["complete"]),
            (["a", "b"], [{"id": "a", "label_ids": ["STARRED"]}], "121", True),
        )

    def test_a_stale_history_start_is_not_found(self):
        self.google.gmail = router({})
        with self.assertRaises(GateError) as caught:
            self.gate().reads.history("5")
        self.assertEqual(caught.exception.category, Category.GMAIL_NOT_FOUND)

    def test_labels_filters_and_drafts_for_a_thread(self):
        self.google.gmail = router(
            {
                "labels": Reply(200, {"labels": [{"id": "Label_1", "name": "Area/Test", "type": "user", "color": {}}]}),
                "settings/filters": Reply(200, {"filter": [{"id": "f1", "criteria": {"from": "a@example.test"}}]}),
                "drafts": Reply(
                    200,
                    {
                        "drafts": [
                            {"id": "r1", "message": {"id": "m1", "threadId": "t1"}},
                            {"id": "r2", "message": {"id": "m2", "threadId": "t2"}},
                        ]
                    },
                ),
            }
        )
        gate = self.gate()
        self.assertEqual(gate.reads.labels()["labels"], [{"id": "Label_1", "name": "Area/Test", "type": "user"}])
        self.assertEqual(gate.reads.filters()["filters"][0]["action"], {})
        self.assertEqual(gate.reads.drafts("t2")["drafts"], [{"id": "r2", "message_id": "m2", "thread_id": "t2"}])


if __name__ == "__main__":
    unittest.main()
