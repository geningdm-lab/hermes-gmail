"""Service configuration, read once at start from a TOML file kept out of Git.

Real names (account, vault item, paths) exist only in the deployment's config file.
"""

import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GMAIL_BASE = "https://gmail.googleapis.com"
GOOGLE_TOKEN_URIS = frozenset({"https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    """The config file is missing, malformed or names a value this build refuses."""


@dataclass(frozen=True)
class VaultConfig:
    item: str
    vault: str
    op: str = "op"
    timeout_seconds: float = 30.0
    attempts: int = 3
    backoff_seconds: float = 1.0
    cooldown_seconds: float = 900.0


@dataclass(frozen=True)
class GoogleConfig:
    """Provider endpoints. Anything but the defaults must be a loopback URL: that is
    how tests reach the local stand-in, and it cannot point the gate at another host."""

    gmail_base: str = GMAIL_BASE
    token_url: str | None = None  # None: the OAuth document's token_uri, one of Google's two


@dataclass(frozen=True)
class BudgetConfig:
    interval_seconds: float = 0.13
    in_flight: int = 3


ROLES = ("read", "classify", "interactive")
_CALLER_NAME = re.compile(r"[a-z0-9_-]{1,32}")


@dataclass(frozen=True)
class CallerConfig:
    """A caller is a peer user plus a name; the name only narrows what that user may do."""

    name: str
    uid: int
    roles: frozenset[str]


@dataclass(frozen=True)
class Config:
    account: str
    state_dir: Path
    vault: VaultConfig
    google: GoogleConfig = field(default_factory=GoogleConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    # The classifier's taxonomy belongs to its owner: the prefix comes from config.
    classify_exclusive_prefix: str | None = None
    journal_retention_days: float = 90.0
    socket: Path | None = None
    socket_mode: int = 0o600
    callers: tuple[CallerConfig, ...] = ()


def is_loopback_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.hostname in _LOOPBACK_HOSTS and not parts.username


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("config file cannot be read as TOML") from error
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    _only_keys(
        raw,
        "",
        {
            "account",
            "state_dir",
            "vault",
            "google",
            "budget",
            "classify",
            "journal",
            "socket",
            "socket_mode",
            "callers",
        },
    )
    account = _string(raw, "account")
    if "@" not in account:
        raise ConfigError("account must be an email address")
    vault_raw = _table(raw, "vault")
    _only_keys(
        vault_raw,
        "vault.",
        {"item", "vault", "op", "timeout_seconds", "attempts", "backoff_seconds", "cooldown_seconds"},
    )
    vault = VaultConfig(
        item=_string(vault_raw, "item", "vault."),
        vault=_string(vault_raw, "vault", "vault."),
        op=_string(vault_raw, "op", "vault.", default="op"),
        timeout_seconds=_positive(vault_raw, "timeout_seconds", "vault.", 30.0),
        attempts=int(_positive(vault_raw, "attempts", "vault.", 3, integer=True)),
        backoff_seconds=_positive(vault_raw, "backoff_seconds", "vault.", 1.0, allow_zero=True),
        cooldown_seconds=_positive(vault_raw, "cooldown_seconds", "vault.", 900.0),
    )
    google_raw = _table(raw, "google", required=False)
    _only_keys(google_raw, "google.", {"gmail_base", "token_url"})
    gmail_base = _string(google_raw, "gmail_base", "google.", default=GMAIL_BASE).rstrip("/")
    if gmail_base != GMAIL_BASE and not is_loopback_url(gmail_base):
        raise ConfigError("google.gmail_base may only be changed to a loopback URL")
    token_url = google_raw.get("token_url")
    if token_url is not None and not (isinstance(token_url, str) and is_loopback_url(token_url)):
        raise ConfigError("google.token_url may only be set to a loopback URL")
    budget_raw = _table(raw, "budget", required=False)
    _only_keys(budget_raw, "budget.", {"interval_seconds", "in_flight"})
    budget = BudgetConfig(
        interval_seconds=_positive(budget_raw, "interval_seconds", "budget.", 0.13, allow_zero=True),
        in_flight=int(_positive(budget_raw, "in_flight", "budget.", 3, integer=True)),
    )
    classify_raw = _table(raw, "classify", required=False)
    _only_keys(classify_raw, "classify.", {"exclusive_prefix"})
    prefix = classify_raw.get("exclusive_prefix")
    if prefix is not None:
        prefix = _string(classify_raw, "exclusive_prefix", "classify.")
    journal_raw = _table(raw, "journal", required=False)
    _only_keys(journal_raw, "journal.", {"retention_days"})
    return Config(
        account=account,
        state_dir=Path(_string(raw, "state_dir")),
        vault=vault,
        google=GoogleConfig(gmail_base=gmail_base, token_url=token_url),
        budget=budget,
        classify_exclusive_prefix=prefix,
        journal_retention_days=_positive(journal_raw, "retention_days", "journal.", 90.0),
        socket=Path(_string(raw, "socket")) if "socket" in raw else None,
        socket_mode=_socket_mode(raw.get("socket_mode", 0o600)),
        callers=_callers(raw.get("callers", [])),
    )


def _socket_mode(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value & ~0o770 or not value & 0o600:
        raise ConfigError("socket_mode must give the owner read and write and nothing to others")
    return value


def _callers(raw: Any) -> tuple[CallerConfig, ...]:
    if not isinstance(raw, list):
        raise ConfigError("callers must be an array of tables")
    callers = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError("each [[callers]] entry must be a table")
        _only_keys(entry, "callers.", {"name", "uid", "roles"})
        name = _string(entry, "name", "callers.")
        if not _CALLER_NAME.fullmatch(name):
            raise ConfigError("callers.name must be 1-32 characters of a-z, 0-9, _ and -")
        uid = entry.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise ConfigError("callers.uid must be a user ID")
        roles = entry.get("roles")
        if not isinstance(roles, list) or not roles or not set(roles) <= set(ROLES):
            raise ConfigError(f"callers.roles must be a non-empty list of {', '.join(ROLES)}")
        callers.append(CallerConfig(name=name, uid=uid, roles=frozenset(roles)))
    if len({c.name for c in callers}) != len(callers):
        raise ConfigError("callers names must be unique")
    return tuple(callers)


def _only_keys(table: dict[str, Any], prefix: str, allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown config key {prefix}{unknown[0]}")


def _table(raw: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = raw.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"config table [{key}] is missing")
    return value


def _string(table: dict[str, Any], key: str, prefix: str = "", *, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{prefix}{key} must be a non-empty string")
    return value


def _positive(
    table: dict[str, Any],
    key: str,
    prefix: str,
    default: float,
    *,
    integer: bool = False,
    allow_zero: bool = False,
) -> float:
    value = table.get(key, default)
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{prefix}{key} must be a positive {'integer' if integer else 'number'}")
    return value
