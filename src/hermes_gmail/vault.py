"""Read the OAuth document from 1Password with `op`, into memory only."""

import json
import os
import subprocess
from dataclasses import dataclass, field

from hermes_gmail.config import GOOGLE_TOKEN_URIS, VaultConfig

_MAX_DOCUMENT_BYTES = 64 * 1024


class VaultReadError(Exception):
    """The vault read failed or returned something unusable. The message is fixed text:
    `op`'s own output is never kept, since it may echo names or a service-account error."""


@dataclass(frozen=True)
class OAuthDocument:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_uri: str

    @classmethod
    def parse(cls, raw: bytes) -> OAuthDocument:
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise VaultReadError("OAuth document is too large")
        try:
            data = json.loads(raw)
        except ValueError:
            raise VaultReadError("OAuth document is not JSON") from None
        if not isinstance(data, dict):
            raise VaultReadError("OAuth document is not a JSON object")
        values = {}
        for key in ("client_id", "client_secret", "refresh_token"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                raise VaultReadError(f"OAuth document lacks {key}")
            values[key] = value
        token_uri = data.get("token_uri", "https://oauth2.googleapis.com/token")
        if token_uri not in GOOGLE_TOKEN_URIS:
            raise VaultReadError("OAuth document names a token URI other than Google's")
        return cls(token_uri=token_uri, **values)


class OpVault:
    def __init__(self, config: VaultConfig) -> None:
        self._config = config

    def read(self) -> OAuthDocument:
        command = [self._config.op, "document", "get", self._config.item, "--vault", self._config.vault]
        try:
            result = subprocess.run(
                command,
                env=os.environ.copy(),
                capture_output=True,
                timeout=self._config.timeout_seconds,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            raise VaultReadError("op did not run to completion") from None
        if result.returncode != 0:
            raise VaultReadError("op exited with an error")
        return OAuthDocument.parse(result.stdout)
