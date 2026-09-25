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


# --- malformed input --------------------------------------------------------


def test_malformed_toml_reports_the_file(base_config_toml):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(_write("this is = not = toml"))


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.toml")
