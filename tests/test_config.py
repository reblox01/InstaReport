from __future__ import annotations

import pytest

from insta_report.config import ConfigError, load_config
from insta_report.support.paths import PathContainmentError
from insta_report.support.redaction import get_registry

from .conftest import FAKE_SESSIONID, scratch_config

#: Local alias so the assertions read as ``load_config(_write(body))``.
_write = scratch_config


def test_loads_a_minimal_valid_config(base_config_toml, proxy_file, session_env):
    config = load_config(_write(base_config_toml))
    assert len(config.accounts) == 1
    assert config.accounts[0].ref == "alpha"
    assert config.accounts[0].username == "reporter.one"
    assert config.accounts[0].sessionid == FAKE_SESSIONID
    assert config.accounts[0].daily_budget == 20


def test_sessionid_is_registered_for_redaction_on_load(
    base_config_toml, proxy_file, session_env
):
    """Loading the config is what arms redaction, before any call site exists."""
    load_config(_write(base_config_toml))
    assert get_registry().scrub(f"cookie {FAKE_SESSIONID}").find(FAKE_SESSIONID) == -1


def test_dirs_are_created_on_load(base_config_toml, proxy_file, session_env):
    config = load_config(_write(base_config_toml))
    assert config.paths.artifacts_dir.is_dir()
    assert config.paths.state_dir.is_dir()


def test_inline_sessionid_warns(base_config_toml, proxy_file, session_env):
    body = base_config_toml.replace(
        'sessionid_env = "IG_SESSIONID_ALPHA"', f'sessionid = "{FAKE_SESSIONID}"'
    )
    with pytest.warns(UserWarning, match="Prefer sessionid_env"):
        config = load_config(_write(body))
    assert config.accounts[0].sessionid == FAKE_SESSIONID


def test_missing_session_env_variable_names_the_variable(
    base_config_toml, proxy_file, clean_env
):
    with pytest.raises(ConfigError, match="IG_SESSIONID_ALPHA"):
        load_config(_write(base_config_toml))


def test_account_without_any_session_credential_errors(base_config_toml, proxy_file):
    body = base_config_toml.replace('sessionid_env = "IG_SESSIONID_ALPHA"\n', "")
    with pytest.raises(ConfigError, match="sessionid or sessionid_env"):
        load_config(_write(body))


def test_no_accounts_at_all_is_rejected(base_config_toml, proxy_file):
    head = base_config_toml.split("[accounts.alpha]")[0]
    tail = "[proxies]" + base_config_toml.split("[proxies]")[1]
    with pytest.raises(ConfigError, match="at least one"):
        load_config(_write(head + tail))


def test_max_concurrent_cannot_exceed_account_count(
    base_config_toml, proxy_file, session_env
):
    body = base_config_toml.replace("max_concurrent = 1", "max_concurrent = 4")
    with pytest.raises(ConfigError, match="exceeds the 1 configured account"):
        load_config(_write(body))


def test_duplicate_account_ref_is_impossible_toml_cannot_express_it(
    base_config_toml, proxy_file, session_env
):
    """Ref is a lease lookup key, so the guarantee has to be real.

    TOML rejects a redefined table, which is why there is no runtime duplicate
    check in the loader. Documented here so the absence is deliberate rather
    than an oversight.
    """
    body = base_config_toml + (
        f'\n[accounts.alpha]\nusername = "second"\nsessionid = "{FAKE_SESSIONID}"\n'
    )
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(_write(body))


def test_wrong_type_names_the_key_and_type(base_config_toml, proxy_file, session_env):
    body = base_config_toml.replace("daily_budget = 20", 'daily_budget = "twenty"')
    with pytest.raises(ConfigError, match=r"accounts\.alpha\] daily_budget must be an integer"):
        load_config(_write(body))


def test_budget_below_one_is_rejected(base_config_toml, proxy_file, session_env):
    body = base_config_toml.replace("daily_budget = 20", "daily_budget = 0")
    with pytest.raises(ConfigError, match="must be >= 1"):
        load_config(_write(body))


def test_boolean_is_not_accepted_as_an_integer(base_config_toml, proxy_file, session_env):
    body = base_config_toml.replace("daily_budget = 20", "daily_budget = true")
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(_write(body))


# --- proxies ----------------------------------------------------------------


def test_open_proxy_harvesting_is_not_expressible(base_config_toml, proxy_file, session_env):
    """The disqualification is enforced, not just documented."""
    body = base_config_toml.replace('source = "file"', 'source = "harvest"')
    with pytest.raises(ConfigError, match="pre-scorched"):
        load_config(_write(body))


def test_file_source_requires_file_path(base_config_toml, proxy_file, session_env):
    body = "\n".join(
        line for line in base_config_toml.splitlines() if "file_path" not in line
    )
    with pytest.raises(ConfigError, match="requires file_path"):
        load_config(_write(body))


def test_missing_proxy_file_is_reported_at_load(base_config_toml, session_env, tmp_path):
    body = base_config_toml.replace("proxies.txt", "does-not-exist.txt")
    with pytest.raises(ConfigError, match="file_path does not exist"):
        load_config(_write(body))


def test_provider_source_requires_a_provider_name(base_config_toml, proxy_file, session_env):
    body = base_config_toml.replace('source = "file"', 'source = "provider"')
    with pytest.raises(ConfigError, match="requires a provider name"):
        load_config(_write(body))


def test_provider_key_is_read_from_the_named_env_var(
    base_config_toml, proxy_file, session_env, monkeypatch
):
    monkeypatch.setenv("IG_PROXY_KEY", "provider-key-value-12345")
    body = base_config_toml.replace('source = "file"', 'source = "provider"')
    body = "\n".join(line for line in body.splitlines() if "file_path" not in line)
    body = body.replace('source = "provider"', 'source = "provider"\nprovider = "brightdata"\nprovider_key_env = "IG_PROXY_KEY"')
    config = load_config(_write(body))
    assert config.proxies.provider == "brightdata"
    assert config.proxies.resolved_key() == "provider-key-value-12345"


# --- api --------------------------------------------------------------------


def test_api_is_disabled_by_default(base_config_toml, proxy_file, session_env):
    config = load_config(_write(base_config_toml))
    assert config.api.enabled is False


def test_enabling_api_without_identities_is_rejected(base_config_toml, proxy_file, session_env):
    body = base_config_toml + "\n[api]\nenabled = true\n"
    with pytest.raises(ConfigError, match="mobile_user_agent"):
        load_config(_write(body))


def test_enabled_api_requires_all_three_identities(base_config_toml, proxy_file, session_env):
    body = base_config_toml + (
        '\n[api]\nenabled = true\nmobile_user_agent = "Instagram 1.2.3 Android"\n'
    )
    with pytest.raises(ConfigError, match="web_user_agent, app_id"):
        load_config(_write(body))


# --- the [run] section ------------------------------------------------------


def test_run_defaults_when_the_section_is_absent(base_config_toml, proxy_file, session_env):
    """Omitting [run] is legal, and gives the documented defaults.

    Not an error: a config that predates the section must still load, and a
    default that only exists when the section is present would mean a config
    file changes behaviour by gaining a comment.
    """
    config = load_config(_write(base_config_toml))
    assert config.run.max_reports == 100
    assert config.run.horizon_hours == 6.0
    assert config.run.max_concurrent is None
    assert config.run.horizon_seconds == 6.0 * 3600.0


def test_run_values_are_read(base_config_toml, proxy_file, session_env):
    body = base_config_toml + """
[run]
max_reports = 7
horizon_hours = 0.25
transient_retries = 5
backoff_seconds = 2
exit_rotations = 4
channel_failure_threshold = 9
floor_gap_seconds = 45
jitter_fraction = 0.5
horizon_fraction = 0.6
narrative_seed = "seeded"
max_detail_length = 120
"""
    run = load_config(_write(body)).run
    assert run.max_reports == 7
    assert run.horizon_hours == 0.25
    assert run.transient_retries == 5
    assert run.backoff_seconds == 2.0
    assert run.exit_rotations == 4
    assert run.channel_failure_threshold == 9
    assert run.floor_gap_seconds == 45.0
    assert run.jitter_fraction == 0.5
    assert run.horizon_fraction == 0.6
    assert run.narrative_seed == "seeded"
    assert run.max_detail_length == 120


def test_max_reports_zero_means_no_ceiling_not_no_reports(
    base_config_toml, proxy_file, session_env
):
    """`max_reports = 0` is never a thing an operator means to type."""
    body = base_config_toml + "\n[run]\nmax_reports = 0\n"
    assert load_config(_write(body)).run.max_reports is None


def test_run_max_concurrent_omitted_defers_to_the_browser(
    base_config_toml, proxy_file, session_env
):
    """None, not a duplicated default.

    A number here would be a second source of truth for "how many sessions at
    once" that can silently disagree with [browser] max_concurrent.
    """
    assert load_config(_write(base_config_toml)).run.max_concurrent is None


def test_run_max_concurrent_cannot_exceed_account_count(
    base_config_toml, proxy_file, session_env
):
    body = base_config_toml + "\n[run]\nmax_concurrent = 4\n"
    with pytest.raises(ConfigError, match="exceeds"):
        load_config(_write(body))


def test_a_pacing_floor_below_the_hard_minimum_is_refused_not_clamped(
    base_config_toml, proxy_file, session_env
):
    """A config that reads "send every 2 seconds" while the tool sends every 8
    is a lie in a file, so it is an error rather than a silent clamp."""
    from insta_report.pacing import MIN_GAP_SECONDS

    body = base_config_toml + "\n[run]\nfloor_gap_seconds = 2\n"
    with pytest.raises(ConfigError, match="below the hard minimum"):
        load_config(_write(body))
    # And the boundary itself is accepted.
    ok = base_config_toml + f"\n[run]\nfloor_gap_seconds = {MIN_GAP_SECONDS}\n"
    assert load_config(_write(ok)).run.floor_gap_seconds == float(MIN_GAP_SECONDS)


def test_a_pacing_floor_above_the_cap_is_refused(
    base_config_toml, proxy_file, session_env
):
    from insta_report.pacing import MAX_GAP_SECONDS

    body = base_config_toml + f"\n[run]\nfloor_gap_seconds = {MAX_GAP_SECONDS + 1}\n"
    with pytest.raises(ConfigError, match="above MAX_GAP_SECONDS"):
        load_config(_write(body))


@pytest.mark.parametrize("key", ["jitter_fraction", "horizon_fraction"])
def test_out_of_range_pacing_fractions_are_refused(
    key, base_config_toml, proxy_file, session_env
):
    body = base_config_toml + f"\n[run]\n{key} = 1.5\n"
    with pytest.raises(ConfigError, match="must be <="):
        load_config(_write(body))


def test_a_whole_number_of_seconds_is_not_a_typo(base_config_toml, proxy_file, session_env):
    """`backoff_seconds = 5` is a TOML integer, and rejecting it would read as
    an error when it is exactly what the operator meant."""
    body = base_config_toml + "\n[run]\nbackoff_seconds = 5\n"
    assert load_config(_write(body)).run.backoff_seconds == 5.0


def test_a_string_where_a_number_belongs_names_the_key(
    base_config_toml, proxy_file, session_env
):
    body = base_config_toml + '\n[run]\nhorizon_hours = "six"\n'
    with pytest.raises(ConfigError, match="horizon_hours must be a number"):
        load_config(_write(body))


def test_a_bolean_where_a_number_belongs_is_refused(
    base_config_toml, proxy_file, session_env
):
    """``True`` is an int in Python, and ``horizon_hours = true`` would become
    1.0 without this check."""
    body = base_config_toml + "\n[run]\nhorizon_hours = true\n"
    with pytest.raises(ConfigError, match="must be a number"):
        load_config(_write(body))


def test_a_zero_channel_failure_threshold_is_refused(
    base_config_toml, proxy_file, session_env
):
    """Zero would mean "disable the channel before its first failure"."""
    body = base_config_toml + "\n[run]\nchannel_failure_threshold = 0\n"
    with pytest.raises(ConfigError, match="channel_failure_threshold must be >= 1"):
        load_config(_write(body))


# --- paths ------------------------------------------------------------------


def test_data_dir_inside_the_repo_is_refused(session_env, proxy_file):
    """Containment, not just "not the repo root".

    Points at a real path inside the checkout. ``resolve_paths`` validates
    before ``ensure()``, so this raises without creating anything -- which is
    what keeps the test from littering the working tree.
    """
    from insta_report.support.paths import find_repo_root

    inside = find_repo_root() / ".runtime-should-never-be-created"
    body = f"""
data_dir = "{inside.as_posix()}"

[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "{proxy_file.as_posix()}"

[browser]
max_concurrent = 1
"""
    with pytest.raises(PathContainmentError, match="outside the work tree"):
        load_config(_write(body))
    assert not inside.exists()


# --- the data_dir override ---------------------------------------------------


def test_the_data_dir_override_wins_over_a_configured_path(
    base_config_toml, proxy_file, session_env, monkeypatch, tmp_path
):
    """One config file, two machines.

    The operator's config names a Windows path that cannot be right in a
    container. Without this the only alternative is a second TOML file kept in
    step by hand, whose sole purpose is to disagree with the first one.
    """
    import os

    from insta_report.config import DATA_DIR_ENV

    elsewhere = tmp_path / "container-data"
    monkeypatch.setenv(DATA_DIR_ENV, str(elsewhere))

    config = load_config(_write(base_config_toml))
    assert config.paths.data_dir == elsewhere.resolve()
    assert os.environ[DATA_DIR_ENV] == str(elsewhere)


def test_the_override_cannot_smuggle_the_ledger_into_the_repository(
    base_config_toml, proxy_file, session_env, monkeypatch
):
    """The override is subject to the same rule as a configured value.

    An override that bypassed the containment check would be a way to put the
    checkpoint ledger somewhere the credential scanner does not look -- and the
    ledger holds the report text, which is the one thing in this tool written in
    the reporter's own voice.
    """
    from insta_report.support.paths import find_repo_root

    from insta_report.config import DATA_DIR_ENV

    inside = find_repo_root() / ".override-should-never-be-created"
    monkeypatch.setenv(DATA_DIR_ENV, str(inside))

    with pytest.raises(PathContainmentError, match="outside the work tree"):
        load_config(_write(base_config_toml))
    assert not inside.exists()


def test_a_relative_override_is_refused_rather_than_guessed(
    base_config_toml, proxy_file, session_env, monkeypatch
):
    """There is no good base for a relative value in an environment variable.

    Every path *in the config* accepts a relative value and resolves it against
    the config file's directory, so that a run's blast radius does not depend on
    the shell it was launched from. That rule cannot transfer: a variable has no
    config file to be relative to. Resolving it against the working directory
    would reintroduce the exact dependence the rest of this file removes, and
    resolving it against the config's directory would be a second, undocumented
    base. So it is refused, naming both absolute forms.

    Found by mutation: an override applied without ``.resolve()`` passed every
    other test in this file, because they all set an absolute path. This is the
    case that was actually uncovered, and it is the one an operator is most
    likely to type.
    """
    from insta_report.config import DATA_DIR_ENV

    for relative in ("./artifacts", "artifacts", "../insta-report-data"):
        monkeypatch.setenv(DATA_DIR_ENV, relative)
        with pytest.raises(ConfigError) as caught:
            load_config(_write(base_config_toml))
        message = str(caught.value)
        assert "relative path" in message, message
        assert "/data" in message, (
            "the refusal should name the form that works, not just reject the"
            f" one that does not: {message}"
        )


def test_an_absolute_override_outside_the_repository_is_accepted(
    base_config_toml, proxy_file, session_env, monkeypatch, tmp_path
):
    """The container's case, and the reason the override exists at all.

    An absolute path that is *not* inside the checkout is the documented way to
    point a container at a volume. Asserted positively because a test that only
    ever refuses says nothing about whether the feature works -- which is how a
    gate ends up blocking everything and reading as a safety improvement.
    """
    from insta_report.config import DATA_DIR_ENV

    outside = tmp_path / "somewhere-else-entirely"
    monkeypatch.setenv(DATA_DIR_ENV, str(outside))
    assert load_config(_write(base_config_toml)).paths.data_dir == outside.resolve()


def test_an_empty_override_is_not_an_override(
    base_config_toml, proxy_file, session_env, monkeypatch
):
    """Whitespace means "not set", and not "the current directory".

    Compose sets this variable unconditionally, and an operator commenting it
    out in an override block leaves it set-but-empty rather than unset. Read as
    a path, ``Path("")`` resolves to the process's working directory -- so the
    ledger would move to wherever the container happened to be started from,
    which is the same class of bug ``_absolutise_paths`` exists to prevent,
    arriving through the new door.

    Asserted by difference rather than by an absolute path, so the test says
    what it means on every platform.
    """
    from insta_report.config import DATA_DIR_ENV

    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    configured = load_config(_write(base_config_toml)).paths.data_dir

    for empty in ("", "   ", "\t\n"):
        monkeypatch.setenv(DATA_DIR_ENV, empty)
        assert load_config(_write(base_config_toml)).paths.data_dir == configured, empty


# --- config-relative paths --------------------------------------------------


def test_a_relative_proxy_path_resolves_against_the_config_not_the_cwd(tmp_path, session_env):
    """A config that means different things in different shells has a blast
    radius the operator never wrote down.

    This is the whole reason: the proxy list sits next to the config, the
    command is run from somewhere else entirely, and the file is still found.
    """
    import os

    home = tmp_path / "project"
    home.mkdir()
    (home / "proxies.txt").write_text("10.0.0.1:8080\n", encoding="utf-8")
    (home / "insta-report.toml").write_text(
        """
[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "proxies.txt"

[browser]
max_concurrent = 1
""",
        encoding="utf-8",
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    previous = os.getcwd()
    os.chdir(elsewhere)
    try:
        config = load_config(home / "insta-report.toml")
    finally:
        os.chdir(previous)
    assert config.proxies.file_path == home / "proxies.txt"


def test_a_relative_data_dir_resolves_against_the_config_not_the_cwd(tmp_path, session_env):
    home = tmp_path / "project"
    home.mkdir()
    (home / "proxies.txt").write_text("10.0.0.1:8080\n", encoding="utf-8")
    (home / "insta-report.toml").write_text(
        """
data_dir = "runtime"

[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "proxies.txt"

[browser]
max_concurrent = 1
""",
        encoding="utf-8",
    )
    config = load_config(home / "insta-report.toml")
    assert config.paths.data_dir == home / "runtime"


def test_an_absolute_path_is_left_exactly_as_written(tmp_path, session_env):
    """Nothing that already worked may change."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (tmp_path / "proxies.txt").write_text("10.0.0.1:8080\n", encoding="utf-8")
    (tmp_path / "insta-report.toml").write_text(
        f"""
[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "{elsewhere.as_posix()}"

[browser]
max_concurrent = 1
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "insta-report.toml")
    assert config.proxies.file_path == elsewhere


def test_a_tilde_path_stays_a_tilde_path(tmp_path, session_env, monkeypatch):
    """``~`` is expanded before the absolutise pass, so a home-relative path
    still lands in the user's home rather than in the config's directory."""
    (tmp_path / "proxies.txt").write_text("10.0.0.1:8080\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "proxies.txt").write_text("10.0.0.2:8080\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    (tmp_path / "insta-report.toml").write_text(
        """
[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "~/proxies.txt"

[browser]
max_concurrent = 1
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "insta-report.toml")
    assert config.proxies.file_path == fake_home / "proxies.txt"


def test_a_missing_relative_proxy_file_reports_the_resolved_path(tmp_path, session_env):
    """The error must name the file it looked for, not the spelling in the TOML.

    An operator who typed ``file_path = "proxies.txt"`` and gets told
    ``proxies.txt does not exist`` looks in the working directory. The path
    that was actually consulted is the only useful thing to print.
    """
    (tmp_path / "insta-report.toml").write_text(
        """
[accounts.alpha]
username = "reporter.one"
sessionid_env = "IG_SESSIONID_ALPHA"

[proxies]
source = "file"
file_path = "not-here.txt"

[browser]
max_concurrent = 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not-here.txt") as excinfo:
        load_config(tmp_path / "insta-report.toml")
    assert str(tmp_path) in str(excinfo.value)


# --- malformed input --------------------------------------------------------


def test_malformed_toml_reports_the_file(base_config_toml):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(_write("this is = not = toml"))


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml")
