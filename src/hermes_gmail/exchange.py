"""One HTTP exchange with a provider: no redirects, bounded body, status returned as data."""

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Any

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # None makes urllib surface the 3xx as an HTTPError, which `send` returns as a
        # status like any other refusal: a redirect is never followed.
        return None


class ExchangeFailed(Exception):
    """No HTTP status came back. `maybe_sent` is False only when the connection was
    refused, so the provider cannot have seen the request."""

    def __init__(self, *, maybe_sent: bool) -> None:
        super().__init__("no HTTP response")
        self.maybe_sent = maybe_sent


class _TooLarge(Exception):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


# (request, timeout seconds) -> a response object urllib's opener would return.
Opener = Callable[[urllib.request.Request, float], Any]


def default_opener() -> Opener:
    opener = urllib.request.build_opener(_NoRedirect())
    return lambda request, timeout: opener.open(request, timeout=timeout)


def send(opener: Opener, request: urllib.request.Request, timeout: float) -> Response:
    try:
        try:
            reply = opener(request, timeout)
        except urllib.error.HTTPError as error:
            with error:
                return Response(error.code, _headers(error.headers), _read(error))
        with reply as stream:
            return Response(stream.status, _headers(stream.headers), _read(stream))
    except urllib.error.URLError as error:
        raise ExchangeFailed(maybe_sent=not isinstance(error.reason, ConnectionRefusedError)) from None
    except ConnectionRefusedError:
        raise ExchangeFailed(maybe_sent=False) from None
    except OSError, http.client.HTTPException, _TooLarge:
        # Timeouts, resets and truncated replies: the request may have reached the provider.
        raise ExchangeFailed(maybe_sent=True) from None


def _read(stream: Any) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise _TooLarge
    return body


def _headers(headers: Message | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {name.lower(): value for name, value in headers.items()}
