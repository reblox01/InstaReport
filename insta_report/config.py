"""Configuration loading and validation.

Deliberately hand-rolled over stdlib ``tomllib`` rather than a validation
framework: the config is small, and an explicit reader produces error messages
that name the exact key that is wrong, which is what an operator needs at 2am
when a selector broke. A framework would trade that away for schema reuse we
do not need.

Secrets have two routes. ``sessionid_env`` names an environment variable and is
the documented path. Inline ``sessionid`` works for a private machine and emits a
warning. Either way the value is registered with the redaction registry on load,
so it can never reach a log line even if a later call site passes it straight
through.
"""

from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .support.paths import Paths, resolve_paths
from .support.redaction import get_registry

__all__ = [
    "ConfigError",
    "AccountConfig",
    "ProxyConfig",
    "BrowserConfig",
    "ApiConfig",
    "AnchorsConfig",
    "Config",
    "load_config",
]


class ConfigError(ValueError):
    """Raised for any malformed or inconsistent configuration."""


@dataclass(frozen=True)
class AccountConfig:
    ref: str
    username: str
    sessionid: str
    enabled: bool = True
    daily_budget: int = 20


@dataclass(frozen=True)
class ProxyConfig:
    source: str  # "file" | "provider"
    file_path: Path | None = None
    provider: str | None = None
    provider_key_env: str | None = None
    sticky_ttl_minutes: int = 240

    def resolved_key(self) -> str | None:
        if not self.provider_key_env:
            return None
        return os.environ.get(self.provider_key_env)


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool = True
    max_concurrent: int = 3
    locale: str = "en-US"
    # None means "derive from the egress IP's geolocation" -- see review
    # finding 10. A residential exit in Frankfurt paired with an en-US
    # timezone is a single loud correlation across three signals.
    timezone: str | None = None
    user_data_dir: Path = field(default_factory=lambda: Path(".browser-profile"))
    navigation_timeout_ms: int = 30_000
    confirmation_timeout_ms: int = 10_000


@dataclass(frozen=True)
class ApiConfig:
    # Off by default and stays off until the T0 probe resolves whether the
    # endpoints exist at all. Enabling it on a guess is how the last version
    # of this tool ended up reporting success while nothing was sent.
    enabled: bool = False
    mobile_user_agent: str | None = None
    web_user_agent: str | None = None
    app_id: str | None = None
    base_url: str = "https://i.instagram.com/api/v1"


@dataclass(frozen=True)
class AnchorsConfig:
    path: Path


@dataclass(frozen=True)
class Config:
    accounts: tuple[AccountConfig, ...]
    proxies: ProxyConfig
    browser: BrowserConfig
    api: ApiConfig
    anchors: AnchorsConfig
    paths: Paths
    source_path: Path

    @property
    def active_accounts(self) -> tuple[AccountConfig, ...]:
        return tuple(a for a in self.accounts if a.enabled)


class _Reader:
    """Typed TOML access that names the offending key on failure."""

    def __init__(self, table: Mapping[str, Any], section: str) -> None:
        self._table = table
        self._section = section

    @property
    def raw(self) -> Mapping[str, Any]:
        return self._table

    def _where(self, key: str) -> str:
        return f"[{self._section}] {key}" if self._section else key

    def table(self, key: str) -> "_Reader":
        value = self._table.get(key)
        if value is None:
            return _Reader({}, f"{self._section}.{key}".lstrip("."))
        if not isinstance(value, dict):
            raise ConfigError(f"{self._where(key)} must be a table, got {type(value).__name__}")
        return _Reader(value, f"{self._section}.{key}".lstrip("."))

    def str_(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        if key not in self._table:
            if required:
                raise ConfigError(f"{self._where(key)} is required but missing")
            return default
        value = self._table[key]
        if not isinstance(value, str):
            raise ConfigError(f"{self._where(key)} must be a string, got {type(value).__name__}")
        if required and not value.strip():
            raise ConfigError(f"{self._where(key)} must not be empty")
        return value

    def int_(self, key: str, default: int, *, minimum: int | None = None) -> int:
        if key not in self._table:
            return default
        value = self._table[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self._where(key)} must be an integer, got {type(value).__name__}")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{self._where(key)} must be >= {minimum}, got {value}")
        return value

    def bool_(self, key: str, default: bool) -> bool:
        if key not in self._table:
            return default
        value = self._table[key]
        if not isinstance(value, bool):
            raise ConfigError(f"{self._where(key)} must be true or false, got {type(value).__name__}")
        return value

    def path_(self, key: str, default: Path | None = None) -> Path | None:
        raw = self.str_(key)
        return Path(raw).expanduser() if raw else default


def _load_accounts(raw: Mapping[str, Any]) -> tuple[AccountConfig, ...]:
    if not raw:
        raise ConfigError(
            "at least one [accounts.<ref>] table is required. "
            "The tool has no anonymous path by design: a report must be filed "
            "from a real, trusted session."
        )
    accounts: list[AccountConfig] = []
    # Duplicate refs are impossible here: TOML rejects a redefined table, and a
    # Python dict cannot hold two identical keys. Ref is used as a lease lookup
    # key, so that guarantee is load-bearing -- hence no runtime check.
    for ref, body in raw.items():
        reader = _Reader(body, f"accounts.{ref}")

        username = reader.str_("username", required=True)
        env_name = reader.str_("sessionid_env")
        inline = reader.str_("sessionid")

        if env_name:
            sessionid = os.environ.get(env_name)
            if not sessionid:
                raise ConfigError(
                    f"[accounts.{ref}] sessionid_env={env_name!r} but that environment "
                    f"variable is unset or empty"
                )
        elif inline:
            warnings.warn(
                f"[accounts.{ref}] has an inline sessionid. Prefer sessionid_env so the "
                "credential stays out of the file entirely.",
                stacklevel=2,
            )
            sessionid = inline
        else:
            raise ConfigError(
                f"[accounts.{ref}] needs either sessionid or sessionid_env"
            )

        get_registry().register(sessionid)

        accounts.append(
            AccountConfig(
                ref=ref,
                username=username,  # type: ignore[arg-type]
                sessionid=sessionid,
                enabled=reader.bool_("enabled", True),
                daily_budget=reader.int_("daily_budget", 20, minimum=1),
            )
        )
    return tuple(accounts)


def _load_proxies(reader: _Reader) -> ProxyConfig:
    source = reader.str_("source", default="file")
    if source not in {"file", "provider"}:
        raise ConfigError(
            f"[proxies] source must be 'file' or 'provider', got {source!r}. "
            "Open-proxy harvesting is deliberately not supported: those addresses "
            "are pre-scorched by every scraper on the internet."
        )
    provider = reader.str_("provider")
    if source == "provider" and not provider:
        raise ConfigError("[proxies] source='provider' requires a provider name")
    file_path = reader.path_("file_path")
    if source == "file" and not file_path:
        raise ConfigError("[proxies] source='file' requires file_path")
    if file_path and not file_path.exists():
        raise ConfigError(f"[proxies] file_path does not exist: {file_path}")

    return ProxyConfig(
        source=source,
        file_path=file_path,
        provider=provider,
        provider_key_env=reader.str_("provider_key_env"),
        sticky_ttl_minutes=reader.int_("sticky_ttl_minutes", 240, minimum=15),
    )


def _load_browser(reader: _Reader, paths: Paths) -> BrowserConfig:
    max_concurrent = reader.int_("max_concurrent", 3, minimum=1)
    user_data_dir = reader.path_("user_data_dir", paths.data_dir / "browser-profile")
    return BrowserConfig(
        headless=reader.bool_("headless", True),
        max_concurrent=max_concurrent,
        locale=reader.str_("locale", default="en-US") or "en-US",
        timezone=reader.str_("timezone"),
        user_data_dir=user_data_dir,  # type: ignore[arg-type]
        navigation_timeout_ms=reader.int_("navigation_timeout_ms", 30_000, minimum=1_000),
        confirmation_timeout_ms=reader.int_("confirmation_timeout_ms", 10_000, minimum=500),
    )


def _load_api(reader: _Reader) -> ApiConfig:
    enabled = reader.bool_("enabled", False)
    mobile_ua = reader.str_("mobile_user_agent")
    web_ua = reader.str_("web_user_agent")
    app_id = reader.str_("app_id")
    if enabled:
        missing = [
            name
            for name, value in (
                ("mobile_user_agent", mobile_ua),
                ("web_user_agent", web_ua),
                ("app_id", app_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"[api] enabled=true requires {', '.join(missing)}. These live in config "
                "rather than code because Instagram's client version strings move weekly."
            )
    return ApiConfig(
        enabled=enabled,
        mobile_user_agent=mobile_ua,
        web_user_agent=web_ua,
        app_id=app_id,
        base_url=reader.str_("base_url", default="https://i.instagram.com/api/v1")
        or "https://i.instagram.com/api/v1",
    )


def load_config(path: str | Path) -> Config:
    """Read, validate, and register secrets for a config file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    with config_path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    root = _Reader(data, "")
    paths = resolve_paths(root.path_("data_dir"))

    # Validate everything before creating anything. A rejected config must not
    # leave a half-built data directory behind.
    accounts = _load_accounts(root.table("accounts").raw)
    proxies = _load_proxies(root.table("proxies"))
    browser = _load_browser(root.table("browser"), paths)
    api = _load_api(root.table("api"))
    anchors = AnchorsConfig(
        path=root.path_("anchors_path", Path(__file__).parent / "data" / "anchors.toml")
    )

    if browser.max_concurrent > len(accounts):
        raise ConfigError(
            f"[browser] max_concurrent={browser.max_concurrent} exceeds the "
            f"{len(accounts)} configured account(s). Every concurrent lease needs its own "
            "account, so this semaphore can never be satisfied. Add accounts or lower it."
        )

    paths.ensure()

    return Config(
        accounts=accounts,
        proxies=proxies,
        browser=browser,
        api=api,
        anchors=anchors,
        paths=paths,
        source_path=config_path,
    )
