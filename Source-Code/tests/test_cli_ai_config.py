"""Tests for the headless CLI's "ai" config block --
tgdatabridge/cli/config.py's _parse_ai/resolve_ai_config, the CLI-config
counterpart of AI Settings (tests/test_ai_settings_dialog.py) and, like
"tls"/"ssh", never carries a secret inline -- see resolve_ai_config's own
docstring on "api_key_env"."""
import pytest

from tgdatabridge.cli.config import CliConfigError, parse_job_config, resolve_ai_config


def _job(ai_block=None):
    raw = {
        "source": {"engine": "MySQL", "host": "h", "port": 3306, "database": "d",
                   "username": "u", "password_env": "SRC_PW"},
        "target": {"engine": "PostgreSQL", "host": "t", "port": 5432, "database": "d",
                   "username": "u", "password_env": "TGT_PW"},
    }
    if ai_block is not None:
        raw["ai"] = ai_block
    return raw


def test_no_ai_block_resolves_to_none():
    config = parse_job_config(_job())
    assert config.ai is None
    assert resolve_ai_config(config) is None


def test_an_ai_block_with_enabled_left_false_resolves_to_none():
    config = parse_job_config(_job({"provider": "claude"}))
    assert resolve_ai_config(config) is None


def test_unknown_ai_fields_are_rejected():
    with pytest.raises(CliConfigError, match="unknown field"):
        parse_job_config(_job({"enabled": True, "bogus": 1}))


def test_an_unknown_provider_is_rejected():
    with pytest.raises(CliConfigError, match="ai.provider"):
        parse_job_config(_job({"provider": "not-a-real-provider"}))


def test_a_minimal_enabled_block_resolves_with_defaults(monkeypatch):
    monkeypatch.setenv("MY_AI_KEY", "sk-test-123")
    config = parse_job_config(_job({"enabled": True, "api_key_env": "MY_AI_KEY"}))
    ai_config = resolve_ai_config(config)
    assert ai_config is not None
    assert ai_config.enabled is True
    assert ai_config.provider == "claude"
    assert ai_config.api_key == "sk-test-123"
    assert ai_config.feature_error_diagnostics is True


def test_a_missing_api_key_env_variable_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    config = parse_job_config(_job({"enabled": True, "api_key_env": "MISSING_KEY"}))
    with pytest.raises(CliConfigError, match="MISSING_KEY"):
        resolve_ai_config(config)


def test_no_api_key_env_at_all_means_an_empty_key():
    config = parse_job_config(_job({"enabled": True, "provider": "compatible", "base_url": "http://x"}))
    ai_config = resolve_ai_config(config)
    assert ai_config.api_key == ""


def test_error_diagnostics_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("MY_AI_KEY", "sk-test")
    config = parse_job_config(_job({
        "enabled": True, "api_key_env": "MY_AI_KEY", "error_diagnostics": False}))
    ai_config = resolve_ai_config(config)
    assert ai_config.feature_error_diagnostics is False


def test_azure_fields_pass_through(monkeypatch):
    monkeypatch.setenv("MY_AI_KEY", "sk-test")
    config = parse_job_config(_job({
        "enabled": True, "provider": "azure_openai", "api_key_env": "MY_AI_KEY",
        "base_url": "https://my-resource.openai.azure.com", "model": "my-deployment",
        "azure_api_version": "2024-08-01",
    }))
    ai_config = resolve_ai_config(config)
    assert ai_config.provider == "azure_openai"
    assert ai_config.base_url == "https://my-resource.openai.azure.com"
    assert ai_config.model == "my-deployment"
    assert ai_config.azure_api_version == "2024-08-01"


def test_the_config_file_never_needs_a_plaintext_api_key():
    """Mirrors _tls_params'/_ssh_params' own guarantee: the only way an
    "ai" block references a secret is by naming an environment variable,
    exactly like password_env."""
    raw = _job({"enabled": True, "provider": "claude", "api_key_env": "SOME_ENV_VAR"})
    assert "sk-" not in str(raw)
