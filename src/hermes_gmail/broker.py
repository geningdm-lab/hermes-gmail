"""The credential broker: one OAuth document and one access token per process.

- The document is read from 1Password once and kept in memory. A failed read is retried
  a bounded number of times, then reads stop for a cooldown that survives a restart.
- One refresh at a time: callers that need a fresh token wait for the same refresh.
- When Google rejects the client or the refresh token, the broker repeats that answer
  without calling anyone for a cooldown, then reads the document again, since the owner
  may have reauthorized in the meantime.
"""

import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hermes_gmail.config import VaultConfig
from hermes_gmail.errors import Category, GateError
from hermes_gmail.exchange import ExchangeFailed, Opener, send
from hermes_gmail.health import Health
from hermes_gmail.vault import OAuthDocument, OpVault, VaultReadError

# Matches the classifier helper this replaces: never use a token in its last minute.
_EXPIRY_MARGIN_SECONDS = 60
_EXPIRES_IN_DEFAULT = 3600
_EXPIRES_IN_MAX = 86400
_TOKEN_TIMEOUT_SECONDS = 30.0
_INVALID_CLIENT_CODES = frozenset({"invalid_client", "unauthorized_client"})


@dataclass(frozen=True)
class _Token:
    value: str = field(repr=False)
    expires_at: float  # monotonic


class TokenClient:
    """The OAuth refresh-token grant against Google's token endpoint."""

    def __init__(self, opener: Opener, health: Health, token_url: str | None = None) -> None:
        self._opener = opener
        self._health = health
        self._token_url = token_url

    def refresh(self, document: OAuthDocument) -> tuple[str, int]:
        payload = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": document.client_id,
                "client_secret": document.client_secret,
                "refresh_token": document.refresh_token,
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - Google's URI or a loopback test URL
            self._token_url or document.token_uri,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            response = send(self._opener, request, _TOKEN_TIMEOUT_SECONDS)
        except ExchangeFailed:
            raise self._health.fail(Category.OAUTH_FAILED, "the token endpoint did not answer") from None
        body = _json_object(response.body)
        if response.status != 200:
            code = body.get("error") if body else None
            if code == "invalid_grant":
                raise self._health.fail(Category.OAUTH_INVALID_GRANT, "Google rejected the refresh token")
            if code in _INVALID_CLIENT_CODES:
                raise self._health.fail(Category.OAUTH_INVALID_CLIENT, "Google rejected the OAuth client")
            raise self._health.fail(Category.OAUTH_FAILED, f"the token endpoint answered {response.status}")
        if body is None:
            raise self._health.fail(Category.OAUTH_FAILED, "the token response is not a JSON object")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise self._health.fail(Category.OAUTH_FAILED, "the token response has no access token")
        expires_in = body.get("expires_in", _EXPIRES_IN_DEFAULT)
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 0 < expires_in <= _EXPIRES_IN_MAX:
            raise self._health.fail(Category.OAUTH_FAILED, "the token response has an unusable expiry")
        return access_token, expires_in


class CooldownStore:
    """When vault reads may resume, as wall-clock seconds in a file. No secret."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "vault-cooldown-until"

    def until(self) -> float | None:
        try:
            value = float(self._path.read_text().strip())
        except OSError, ValueError:
            return None
        return value

    def start(self, until: float) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".cooldown-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(f"{until}\n")
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class CredentialBroker:
    def __init__(
        self,
        *,
        vault: OpVault,
        tokens: TokenClient,
        cooldown: CooldownStore,
        config: VaultConfig,
        health: Health,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._vault = vault
        self._tokens = tokens
        self._cooldown = cooldown
        self._config = config
        self._health = health
        self._wall = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._document: OAuthDocument | None = None
        self._token: _Token | None = None
        self._rejected: tuple[Category, float] | None = None  # (category, monotonic end of hold)

    def token(self) -> str:
        with self._lock:
            if self._token is not None and self._monotonic() < self._token.expires_at:
                return self._token.value
            return self._refresh_locked()

    def force_refresh(self) -> None:
        """Refresh now, whatever the held token's expiry: the watchdog's check."""
        with self._lock:
            self._refresh_locked()

    def invalidate(self, value: str) -> None:
        """Gmail answered 401 to `value`: drop it, unless a newer token already replaced it."""
        with self._lock:
            if self._token is not None and self._token.value == value:
                self._token = None

    def cooldown_until(self) -> float | None:
        until = self._cooldown.until()
        return until if until is not None and until > self._wall() else None

    def _refresh_locked(self) -> str:
        if self._rejected is not None:
            category, hold_end = self._rejected
            if self._monotonic() < hold_end:
                raise self._health.fail(category, "Google rejected the credential; not retrying yet")
            self._rejected = None
        if self._document is None:
            self._document = self._read_vault_locked()
        try:
            value, expires_in = self._tokens.refresh(self._document)
        except GateError as error:
            if error.category in (Category.OAUTH_INVALID_GRANT, Category.OAUTH_INVALID_CLIENT):
                self._document = None
                self._token = None
                self._rejected = (error.category, self._monotonic() + self._config.cooldown_seconds)
            raise
        self._health.count("token_refreshes")
        margin = _EXPIRY_MARGIN_SECONDS if expires_in > 2 * _EXPIRY_MARGIN_SECONDS else 0
        self._token = _Token(value, self._monotonic() + expires_in - margin)
        return value

    def _read_vault_locked(self) -> OAuthDocument:
        if self.cooldown_until() is not None:
            raise self._health.fail(Category.VAULT_UNAVAILABLE, "1Password reads are cooling down")
        for attempt in range(self._config.attempts):
            self._health.count("vault_reads")
            try:
                document = self._vault.read()
            except VaultReadError:
                if attempt + 1 < self._config.attempts:
                    self._sleep(self._config.backoff_seconds * 2**attempt)
                continue
            self._cooldown.clear()
            return document
        self._cooldown.start(self._wall() + self._config.cooldown_seconds)
        raise self._health.fail(Category.VAULT_UNAVAILABLE, "1Password read failed; reads are cooling down")


def _json_object(raw: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
