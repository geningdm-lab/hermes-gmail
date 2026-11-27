import unittest
from pathlib import Path

from hermes_gmail.config import ConfigError, load_config, parse_config

MINIMAL = {
    "account": "owner@example.test",
    "state_dir": "/tmp/hermes-gmail-test",
    "vault": {"item": "synthetic-item", "vault": "synthetic-vault"},
}


class ConfigTests(unittest.TestCase):
    def test_defaults_point_at_google(self):
        config = parse_config(MINIMAL)
        self.assertEqual(config.google.gmail_base, "https://gmail.googleapis.com")
        self.assertIsNone(config.google.token_url)
        self.assertEqual((config.budget.interval_seconds, config.budget.in_flight), (0.13, 3))

    def test_endpoints_can_only_move_to_loopback(self):
        # Config is how tests reach the stand-in; it must not be a way to send the
        # refresh token or mail to another host.
        for google in (
            {"gmail_base": "https://gmail.example.test"},
            {"token_url": "https://token.example.test/token"},
            {"token_url": "http://user@127.0.0.1:1/token"},
        ):
            with self.subTest(google), self.assertRaises(ConfigError):
                parse_config({**MINIMAL, "google": google})
        parse_config(
            {**MINIMAL, "google": {"gmail_base": "http://127.0.0.1:8080", "token_url": "http://localhost:1/t"}}
        )

    def test_the_example_config_is_valid(self):
        config = load_config(Path(__file__).resolve().parent.parent / "docs" / "config.example.toml")
        self.assertEqual({c.name for c in config.callers}, {"classifier", "reader", "agent"})

    def test_unknown_keys_and_bad_values_are_refused(self):
        for raw in (
            {**MINIMAL, "acount": "typo"},
            {**MINIMAL, "vault": {**MINIMAL["vault"], "attempts": 0}},
            {**MINIMAL, "vault": {**MINIMAL["vault"], "attempts": True}},
            {**MINIMAL, "account": "not-an-address"},
            {**MINIMAL, "socket_mode": 0o666},
            {**MINIMAL, "callers": [{"name": "a", "uid": 1, "roles": ["admin"]}]},
            {**MINIMAL, "callers": [{"name": "a", "uid": 1, "roles": ["read"]}] * 2},
        ):
            with self.subTest(raw), self.assertRaises(ConfigError):
                parse_config(raw)


if __name__ == "__main__":
    unittest.main()
