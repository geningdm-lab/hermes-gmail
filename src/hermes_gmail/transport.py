"""Gmail REST calls with the retry rules of docs/design.md (Transport).

- Reads retry 429, 5xx and a rate-limit 403 with backoff and jitter, honour
  Retry-After, and stop at the operation deadline.
- Writes are never retried. A write whose outcome is unknown is `uncertain`; the
  caller reads back or journals.
- One 401 triggers one refresh and one repeat: Gmail did not act on a request it
  answered with 401.
"""

import json
import random
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hermes_gmail.broker import CredentialBroker
from hermes_gmail.budget import Budget
from hermes_gmail.errors import Category, GateError
from hermes_gmail.exchange import ExchangeFailed, Opener, Response, send
from hermes_gmail.health import Health

PREFIX = "/gmail/v1/users/me/"
_PATH = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")
_REQUEST_TIMEOUT_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 32.0
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})

Params = Mapping[str, str | int | Sequence[str]]


class Transport:
    def __init__(
        self,
        *,
        base_url: str,
        broker: CredentialBroker,
        budget: Budget,
        health: Health,
        opener: Opener,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._base = base_url.rstrip("/") + PREFIX
        self._broker = broker
        self._budget = budget
        self._health = health
        self._opener = opener
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter

    def read(self, path: str, params: Params | None = None, *, deadline: float) -> dict[str, Any]:
        url = self._url(path, params)
        attempt = 0
        while True:
            try:
                response = self._exchange("GET", url, None, deadline)
            except ExchangeFailed:
                retry_category = Category.GMAIL_FAILED
                wait = self._backoff(attempt, None)
            else:
                if 200 <= response.status < 300:
                    self._health.success("read")
                    return self._body(response)
                retry_category = _retryable(response)
                if retry_category is None:
                    raise self._refusal(response)
                wait = self._backoff(attempt, response.headers.get("retry-after"))
            if self._monotonic() + wait >= deadline:
                if retry_category is Category.GMAIL_QUOTA:
                    raise self._health.fail(Category.GMAIL_QUOTA, "Gmail is rate limiting; retries ran out of time")
                raise self._health.fail(Category.GMAIL_FAILED, "Gmail kept failing; retries ran out of time")
            self._sleep(wait)
            attempt += 1

    def write(self, method: str, path: str, body: Mapping[str, Any], *, deadline: float) -> dict[str, Any]:
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise self._health.fail(Category.REFUSED, "not a write method")
        url = self._url(path, None)
        data = None if method == "DELETE" else json.dumps(body).encode()
        try:
            response = self._exchange(method, url, data, deadline)
        except ExchangeFailed as failure:
            if failure.maybe_sent:
                raise self._health.fail(Category.UNCERTAIN, "the write may or may not have happened") from None
            raise self._health.fail(Category.GMAIL_FAILED, "Gmail could not be reached; nothing was written") from None
        if 200 <= response.status < 300:
            self._health.success("write")
            return self._body(response)
        if response.status >= 500:
            raise self._health.fail(Category.UNCERTAIN, "Gmail failed mid-write; the write may have happened")
        if _retryable(response) is Category.GMAIL_QUOTA:
            raise self._health.fail(Category.GMAIL_QUOTA, "Gmail is rate limiting; nothing was written")
        raise self._refusal(response)

    def _exchange(self, method: str, url: str, data: bytes | None, deadline: float) -> Response:
        """One request, plus one repeat after a refresh when Gmail answers 401."""
        for refreshed in (False, True):
            token = self._broker.token()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise self._health.fail(Category.GMAIL_FAILED, "the operation deadline passed")
            request = urllib.request.Request(  # noqa: S310 - fixed Gmail base or a loopback test URL
                url,
                data=data,
                method=method,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with self._budget.slot(deadline):
                self._health.count("requests")
                response = send(self._opener, request, min(_REQUEST_TIMEOUT_SECONDS, remaining))
            if response.status != 401:
                return response
            self._broker.invalidate(token)
            if refreshed:
                break
        raise self._health.fail(Category.OAUTH_FAILED, "Gmail rejected a freshly refreshed token")

    def _url(self, path: str, params: Params | None) -> str:
        if not _PATH.fullmatch(path):
            raise self._health.fail(Category.REFUSED, "not a Gmail API path")
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None and retry_after.isdigit():
            return float(retry_after)
        ceiling = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2**attempt)
        return ceiling * (0.5 + self._jitter() / 2)

    def _body(self, response: Response) -> dict[str, Any]:
        if not response.body:
            return {}
        try:
            value = json.loads(response.body)
        except ValueError:
            raise self._health.fail(Category.GMAIL_FAILED, "Gmail answered with something other than JSON") from None
        if not isinstance(value, dict):
            raise self._health.fail(Category.GMAIL_FAILED, "Gmail answered with something other than an object")
        return value

    def _refusal(self, response: Response) -> GateError:
        if response.status == 404:
            return self._health.fail(Category.GMAIL_NOT_FOUND, "Gmail has no such item")
        if response.status == 403 and _reasons(response) & _SCOPE_REASONS:
            return self._health.fail(Category.GMAIL_SCOPE, "the OAuth grant lacks the scope for this call")
        return self._health.fail(Category.GMAIL_FAILED, f"Gmail answered {response.status}")


def _retryable(response: Response) -> Category | None:
    if response.status == 429:
        return Category.GMAIL_QUOTA
    if response.status == 403 and _reasons(response) & _RATE_LIMIT_REASONS:
        return Category.GMAIL_QUOTA
    if response.status >= 500:
        return Category.GMAIL_FAILED
    return None


def _reasons(response: Response) -> set[str]:
    """Reason codes from Google's error object. Only codes are read, never messages."""
    try:
        error = json.loads(response.body).get("error", {})
    except ValueError, AttributeError:
        return set()
    if not isinstance(error, dict):
        return set()
    reasons: set[str] = set()
    for key in ("errors", "details"):
        items = error.get(key)
        if isinstance(items, list):
            reasons.update(
                item["reason"] for item in items if isinstance(item, dict) and isinstance(item.get("reason"), str)
            )
    return reasons
