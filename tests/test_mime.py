"""Acceptance row 5: text from MIME without guessing and without fetching anything."""

import base64
import unittest

from hermes_gmail.mime import extract_text


def b64(text, charset="utf-8"):
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def part(mime_type, text=None, *, charset=None, data=None, **extra):
    headers = [{"name": "Content-Type", "value": f"{mime_type}; charset={charset}"}] if charset else []
    body = {"data": data if data is not None else b64(text, charset or "utf-8")} if text or data else {}
    return {"mimeType": mime_type, "headers": headers, "body": body, **extra}


def multipart(*parts, kind="alternative"):
    return {"mimeType": f"multipart/{kind}", "parts": list(parts)}


class ExtractText(unittest.TestCase):
    def test_cases(self):
        cases = [
            (
                "plain wins over html",
                multipart(part("text/plain", "Plain body"), part("text/html", "<p>Html body</p>")),
                ("Plain body", "plain", False, False),
            ),
            (
                "html only keeps visible text, drops script and style, decodes entities",
                part(
                    "text/html",
                    "<html><head><style>p{}</style></head><body><p>Hello&nbsp;&amp; welcome</p>"
                    "<script>steal()</script><div>Second line</div></body></html>",
                ),
                ("Hello & welcome\nSecond line", "html", False, False),
            ),
            (
                "image only has no text and is not a decode failure",
                multipart(part("image/png", data="iVBORw0KGgo"), kind="related"),
                ("", "none", False, False),
            ),
            ("malformed base64", part("text/plain", data="@@not*base64@@"), ("", "plain", False, True)),
            (
                "unknown charset falls back with replacement and says so",
                part("text/plain", data=b64("café"), charset="x-unknown-charset"),
                ("café", "plain", False, True),
            ),
            (
                "declared charset is honoured",
                part("text/plain", "привет", charset="koi8-r"),
                ("привет", "plain", False, False),
            ),
            (
                "attachment named text/plain is never read",
                multipart(
                    part("text/plain", "attachment text", filename="notes.txt"), part("text/html", "<p>Body</p>")
                ),
                ("Body", "html", False, False),
            ),
            (
                "body kept out of line is a decode failure, not empty success",
                {"mimeType": "text/plain", "body": {"attachmentId": "ATT1", "size": 900000}},
                ("", "plain", False, True),
            ),
        ]
        for name, payload, (text, source, truncated, failed) in cases:
            with self.subTest(name):
                result = extract_text(payload)
                self.assertEqual(
                    (result.text, result.source, result.truncated, result.decode_failed),
                    (text, source, truncated, failed),
                )

    def test_truncation_is_flagged_at_the_cap(self):
        result = extract_text(part("text/plain", "x" * 101), cap=100)
        self.assertEqual((len(result.text), result.truncated), (100, True))
        self.assertFalse(extract_text(part("text/plain", "x" * 100), cap=100).truncated)


if __name__ == "__main__":
    unittest.main()
