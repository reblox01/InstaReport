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
from typing import Any, Mapping, overload

from .support.paths import Paths, resolve_paths
from .support.redaction import get_registry

__all__ = [
    "ConfigError",
    "AccountConfig",
    "ProxyConfig",
    "BrowserConfig",
    "ApiConfig",
    "AnchorsConfig",
    "RunConfig",
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
    #: The account's own numeric ``ds_user_id``.
    #:
    #: Optional, and deliberately so. It is needed to address *this* account by id
    #: -- which the T0 probe requires, because a request path containing an
    #: unsubstituted ``{user_id}`` is a malformed URL, and a malformed URL's 404 is
    #: indistinguishable from a missing route. It is not a secret, so unlike
    #: ``sessionid`` it is not read from the environment.
    #:
    #: It is the *target's* id that a report is filed against, and that comes from
    #: the target record, not from here. This one identifies the reporting account.
    user_id: str | None = None


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
class RunConfig:
    """How a run is scheduled, and how loudly it narrates.

    Split from the runner's own :class:`~insta_report.runner.RunOptions` on
    purpose. ``RunOptions`` is per-invocation and the CLI builds it from these
    values plus the flags; this is the durable half an operator edits once. The
    two are kept separate so "what did the flag do" and "what did the file say"
    are answerable separately, which is the same reason ``plan()`` and the run
    share their work-queue computation instead of each having its own idea.
    """

    #: Hard cap on dispatches for one run. ``None`` means "spend the budget",
    #: which the CLI allows but never defaults to: a run with no ceiling is a
    #: run whose blast radius is whatever the account's daily budget happens to
    #: be, decided by a file the operator may not remember editing.
    max_reports: int | None = 100
    #: Wall time after which no new work starts. An attended run (D12): a stop
    #: condition, not a schedule to fill.
    horizon_hours: float = 6.0
    transient_retries: int = 2
    backoff_seconds: float = 5.0
    #: Times an expired exit is replaced before the channel is blamed for it.
    exit_rotations: int = 2
    channel_failure_threshold: int = 3
    #: ``None`` defers to ``[browser] max_concurrent``, which is the ceiling the
    #: browser can actually honour. A separate number here would be a second
    #: source of truth for "how many sessions at once".
    max_concurrent: int | None = None

    # -- pacing ----------------------------------------------------------
    # Defaults are :data:`insta_report.pacing.PacingConfig`'s, restated here so
    # the example config can show an operator the whole surface. Validated
    # against MIN_GAP_SECONDS and MAX_GAP_SECONDS on load: a floor below the
    # hard minimum is silently raised by the pacer, and a config that reads as
    # "send every 2 seconds" while the tool sends every 8 is a lie in a file.
    floor_gap_seconds: float = 30.0
    jitter_fraction: float = 0.25
    #: Per-step damping factor, NOT the fraction of the horizon the run will
    #: use. It compounds, because the gap is recomputed against a shrinking
    #: horizon each step. See :class:`insta_report.pacing.PacingConfig`.
    horizon_fraction: float = 0.85

    # -- narrative -------------------------------------------------------
    #: Extra string mixed into narrative selection so a retried target produces
    #: byte-identical text. Empty means "derive from the target", which is
    #: already deterministic; setting it is for an operator who needs the same
    #: rotation to hold across a re-run of a different target file.
    narrative_seed: str = ""
    max_detail_length: int = 400

    @property
    def horizon_seconds(self) -> float:
        return self.horizon_hours * 3600.0


@dataclass(frozen=True)
class Config:
    accounts: tuple[AccountConfig, ...]
    proxies: ProxyConfig
    browser: BrowserConfig
    api: ApiConfig
    anchors: AnchorsConfig
    run: RunConfig
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

    def float_(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        if key not in self._table:
            return default
        value = self._table[key]
        # A TOML integer is a perfectly reasonable thing to write for a
        # seconds-valued key, and rejecting it would make `backoff_seconds = 5`
        # an error that reads like a typo when it is not.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"{self._where(key)} must be a number, got {type(value).__name__}"
            )
        number = float(value)
        if minimum is not None and number < minimum:
            raise ConfigError(f"{self._where(key)} must be >= {minimum}, got {number}")
        if maximum is not None and number > maximum:
            raise ConfigError(f"{self._where(key)} must be <= {maximum}, got {number}")
        return number

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

    @overload
    def path_(self, key: str) -> Path | None: ...

    @overload
    def path_(self, key: str, default: Path) -> Path: ...

    def path_(self, key: str, default: Path | None = None) -> Path | None:
        """A path from this table, or *default* when the key is absent.

        Overloaded rather than returning a bare ``Path | None``, because the two
        call sites mean different things by the result: one wants "not
        configured, carry on" and the other wants a path it can use without
        checking. Collapsing them to one optional return made the second caller
        cast its way past the check, which is exactly the cast that hides a
        missing key until something dereferences it.
        """
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
                user_id=reader.str_("user_id"),
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
        user_data_dir=user_data_dir,
        navigation_timeout_ms=reader.int_("navigation_timeout_ms", 30_000, minimum=1_000),
        confirmation_timeout_ms=reader.int_("confirmation_timeout_ms", 10_000, minimum=500),
    )


def _load_run(reader: _Reader) -> RunConfig:
    """Read ``[run]``.

    The pacing numbers are validated against the pacer's own constants here
    rather than inside the pacer, because the failure an operator needs to see
    is "your config says 2 seconds and the tool will use 8", and that is a
    config error. A silent clamp to the floor would leave a file that reads one
    way and behaves another, which is the class of bug this project exists to
    stop committing.
    """
    from .pacing import MAX_GAP_SECONDS, MIN_GAP_SECONDS

    max_reports_raw = reader.int_("max_reports", 100, minimum=0)
    floor_gap = reader.float_("floor_gap_seconds", 30.0, minimum=0.0)
    if floor_gap < MIN_GAP_SECONDS:
        raise ConfigError(
            f"[run] floor_gap_seconds={floor_gap} is below the hard minimum of "
            f"{MIN_GAP_SECONDS:.0f}s. Two reports are never sent closer than that "
            "because a burst is the shape of a challenge; raise the floor rather "
            "than the tool ignoring you."
        )
    if floor_gap > MAX_GAP_SECONDS:
        raise ConfigError(
            f"[run] floor_gap_seconds={floor_gap} is above MAX_GAP_SECONDS="
            f"{MAX_GAP_SECONDS:.0f}s, so every gap would be clamped down to a value "
            "your config did not ask for. Lower it, or raise the cap deliberately."
        )
    jitter = reader.float_("jitter_fraction", 0.25, minimum=0.0, maximum=1.0)
    horizon_fraction = reader.float_("horizon_fraction", 0.85, minimum=0.01, maximum=0.99)

    # None rather than a default, so the CLI can tell "not set here" from "set
    # to the same number the browser section would have said". The alternative
    # -- a duplicated default in two places that must be kept in step -- is
    # exactly how a config ends up with two max_concurrent values that disagree.
    max_concurrent: int | None = None
    if "max_concurrent" in reader.raw:
        max_concurrent = reader.int_("max_concurrent", 1, minimum=1)

    return RunConfig(
        # 0 is the one value that means "no ceiling" rather than "no reports",
        # because `max_reports = 0` is never a thing an operator means to type.
        max_reports=None if max_reports_raw == 0 else max_reports_raw,
        horizon_hours=reader.float_("horizon_hours", 6.0, minimum=0.01, maximum=168.0),
        transient_retries=reader.int_("transient_retries", 2, minimum=0),
        backoff_seconds=reader.float_("backoff_seconds", 5.0, minimum=0.0),
        exit_rotations=reader.int_("exit_rotations", 2, minimum=0),
        channel_failure_threshold=reader.int_("channel_failure_threshold", 3, minimum=1),
        max_concurrent=max_concurrent,
        floor_gap_seconds=floor_gap,
        jitter_fraction=jitter,
        horizon_fraction=horizon_fraction,
        narrative_seed=reader.str_("narrative_seed") or "",
        max_detail_length=reader.int_("max_detail_length", 400, minimum=1),
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


#: Every path a config may name, as ``(section, key)`` where a section of ``""``
#: is the root table. Listed rather than discovered, because a path that is
#: resolved against the wrong base is a path that means something different in
#: a different directory -- and the whole point of naming them here is that the
#: list is short enough to be checked by reading it.
CONFIG_PATH_KEYS: tuple[tuple[str, str], ...] = (
    ("", "data_dir"),
    ("", "anchors_path"),
    ("proxies", "file_path"),
    ("browser", "user_data_dir"),
)


def _absolutise_paths(data: dict[str, Any], base: Path) -> None:
    """Rewrite every relative path in *data* so it is relative to *base*.

    Relative to the **config file's directory**, not the working directory.

    ```
        ~/tools/insta-report/
          config.toml        <- says  file_path = "proxies.txt"
          proxies.txt        <- what the operator meant

        $ cd /tmp && insta-report run --config ~/tools/insta-report/config.toml
        cwd-relative:  /tmp/proxies.txt          (not found, or worse: found)
        config-relative: ~/tools/insta-report/proxies.txt   (what was meant)
    ```

    The failure this prevents is not a crash. It is a run that picks up a
    *different* proxy list, a different data directory or a different browser
    profile depending on which shell it was launched from, and therefore a run
    whose blast radius depends on something the operator never wrote down.
    Absolute paths are left alone, so nothing that already worked changes.
    """
    for section, key in CONFIG_PATH_KEYS:
        table = data if section == "" else data.get(section)
        if not isinstance(table, dict) or key not in table:
            continue
        raw = table[key]
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            continue
        table[key] = str((base / candidate).resolve())


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

    _absolutise_paths(data, config_path.parent)

    root = _Reader(data, "")
    paths = resolve_paths(root.path_("data_dir"))

    # Validate everything before creating anything. A rejected config must not
    # leave a half-built data directory behind.
    accounts = _load_accounts(root.table("accounts").raw)
    proxies = _load_proxies(root.table("proxies"))
    browser = _load_browser(root.table("browser"), paths)
    api = _load_api(root.table("api"))
    run = _load_run(root.table("run"))
    anchors = AnchorsConfig(
        path=root.path_("anchors_path", Path(__file__).parent / "data" / "anchors.toml")
    )

    if browser.max_concurrent > len(accounts):
        raise ConfigError(
            f"[browser] max_concurrent={browser.max_concurrent} exceeds the "
            f"{len(accounts)} configured account(s). Every concurrent lease needs its own "
            "account, so this semaphore can never be satisfied. Add accounts or lower it."
        )

    if run.max_concurrent is not None and run.max_concurrent > len(accounts):
        raise ConfigError(
            f"[run] max_concurrent={run.max_concurrent} exceeds the "
            f"{len(accounts)} configured account(s). The workers are bounded by "
            "eligible accounts anyway, so this number can only be wrong."
        )

    paths.ensure()

    return Config(
        accounts=accounts,
        proxies=proxies,
        browser=browser,
        api=api,
        anchors=anchors,
        run=run,
        paths=paths,
        source_path=config_path,
    )
