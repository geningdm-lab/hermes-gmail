"""Transport rules: read retries, no blind write retries, one 401 repeat, the shared budget."""

import dataclasses
import socket
import threading
import time
import unittest

from hermes_gmail.errors import Category, GateError
from hermes_gmail.gate import Gate
from tests.harness import GateTestCase, Reply


def replies(*sequence):
    """A Gmail handler that answers in order and repeats the last reply."""
    items = list(sequence)

    def handler(seen):
        return items.pop(0) if len(items) > 1 else items[0]

    return handler


class Reads(GateTestCase):
    def test_transient_errors_are_retried_honouring_retry_after(self):
        self.google.gmail = replies(Reply(503), Reply(429, {}, {"Retry-After": "7"}), Reply(200, {"historyId": "9"}))
        gate = self.gate()
        self.assertEqual(gate.transport.read("profile", deadline=gate.deadline(60)), {"historyId": "9"})
        self.assertEqual(len(self.google.gmail_requests()), 3)
        self.assertIn(7.0, self.clock.sleeps)

    def test_retries_stop_at_the_deadline(self):
        self.google.gmail = replies(Reply(503))
        gate = self.gate()
        with self.assertRaises(GateError) as caught:
            gate.transport.read("profile", deadline=gate.deadline(5))
        self.assertEqual(caught.exception.category, Category.GMAIL_FAILED)
        self.assertLessEqual(sum(self.clock.sleeps), 5)

    def test_401_refreshes_once_and_repeats_once(self):
        self.google.gmail = replies(Reply(401), Reply(200, {"historyId": "1"}))
        gate = self.gate()
        gate.transport.read("profile", deadline=gate.deadline(10))
        auth = [s.authorization for s in self.google.gmail_requests()]
        self.assertEqual(auth, ["Bearer access-1", "Bearer access-2"])

    def test_a_second_401_is_an_oauth_failure_not_a_loop(self):
        self.google.gmail = replies(Reply(401))
        gate = self.gate()
        with self.assertRaises(GateError) as caught:
            gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual(caught.exception.category, Category.OAUTH_FAILED)
        self.assertEqual(len(self.google.gmail_requests()), 2)

    def test_paths_outside_the_gmail_api_are_refused_before_any_call(self):
        gate = self.gate()
        for path in ("../token", "https://elsewhere.test/x", "messages?x=1", ""):
            with self.subTest(path), self.assertRaises(GateError) as caught:
                gate.transport.read(path, deadline=gate.deadline(10))
            self.assertEqual(caught.exception.category, Category.REFUSED)
        self.assertEqual(self.google.seen, [])
        self.assertEqual(self.op.calls(), [])


class Writes(GateTestCase):
    def write(self, gate):
        with self.assertRaises(GateError) as caught:
            gate.transport.write("POST", "messages/batchModify", {"ids": ["a"]}, deadline=gate.deadline(30))
        return caught.exception.category

    def test_writes_are_never_retried(self):
        # A retried write after a 5xx could apply twice; the caller reads back instead.
        cases = [
            (Reply(503), Category.UNCERTAIN),
            (Reply(500), Category.UNCERTAIN),
            (Reply(429), Category.GMAIL_QUOTA),
            (Reply(400), Category.GMAIL_FAILED),
        ]
        for reply, expected in cases:
            with self.subTest(reply.status):
                self.google.seen.clear()
                self.google.gmail = replies(reply)
                self.assertEqual(self.write(self.gate()), expected)
                self.assertEqual(len(self.google.gmail_requests()), 1)

    def test_a_write_answered_401_is_repeated_once(self):
        self.google.gmail = replies(Reply(401), Reply(200, {}))
        gate = self.gate()
        gate.transport.write("POST", "messages/batchModify", {"ids": ["a"]}, deadline=gate.deadline(30))
        self.assertEqual(len(self.google.gmail_requests()), 2)

    def test_unreachable_gmail_means_nothing_was_written(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        config = self.config()
        config = dataclasses.replace(
            config, google=dataclasses.replace(config.google, gmail_base=f"http://127.0.0.1:{closed_port}")
        )
        gate = Gate(config, wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)
        self.assertEqual(self.write(gate), Category.GMAIL_FAILED)


class SharedBudget(GateTestCase):
    def test_at_most_three_requests_in_flight(self):
        active, peak, lock = [0], [0], threading.Lock()

        def slow(seen):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.2)
            with lock:
                active[0] -= 1
            return Reply(200, {})

        self.google.gmail = slow
        gate = self.gate()
        gate.broker.token()  # the refresh is not part of what is measured
        threads = [
            threading.Thread(target=gate.transport.read, args=("profile",), kwargs={"deadline": gate.deadline(30)})
            for _ in range(7)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 3)

    def test_request_starts_are_paced(self):
        self.google.gmail = replies(Reply(200, {}))
        config = self.config()
        config = dataclasses.replace(config, budget=dataclasses.replace(config.budget, interval_seconds=0.13))
        gate = Gate(config, wall_clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep)
        for _ in range(3):
            gate.transport.read("profile", deadline=gate.deadline(10))
        self.assertEqual([round(s, 2) for s in self.clock.sleeps], [0.13, 0.13])


if __name__ == "__main__":
    unittest.main()
