"""Tests for tgdatabridge.utils.ai_settings -- persisted AI configuration,
with the API key encrypted at rest separately from everything else (see
that module's own docstring for why)."""
from tgdatabridge.utils import ai_settings


def test_loading_with_nothing_saved_returns_defaults(tmp_path):
    settings = ai_settings.load_ai_settings(base_dir=tmp_path)
    assert settings == ai_settings.AiSettings()
    assert settings.enabled is False


def test_a_corrupted_settings_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "ai_settings.json").write_text("not valid json{{{", encoding="utf-8")
    assert ai_settings.load_ai_settings(base_dir=tmp_path) == ai_settings.AiSettings()


def test_save_and_load_round_trip(tmp_path):
    saved = ai_settings.AiSettings(
        enabled=True, provider="openai", base_url="https://api.openai.com",
        model="gpt-4o", feature_data_quality=False,
    )
    ai_settings.save_ai_settings(saved, base_dir=tmp_path)
    loaded = ai_settings.load_ai_settings(base_dir=tmp_path)
    assert loaded == saved


def test_unknown_fields_in_the_file_are_ignored(tmp_path):
    import json
    path = tmp_path / "ai_settings.json"
    path.write_text(json.dumps({"enabled": True, "provider": "claude", "future_field": 123}),
                     encoding="utf-8")
    settings = ai_settings.load_ai_settings(base_dir=tmp_path)
    assert settings.enabled is True
    assert not hasattr(settings, "future_field")


def test_the_settings_file_never_contains_the_api_key(tmp_path):
    ai_settings.save_api_key("sk-super-secret", base_dir=tmp_path)
    ai_settings.save_ai_settings(ai_settings.AiSettings(enabled=True), base_dir=tmp_path)
    raw = (tmp_path / "ai_settings.json").read_text(encoding="utf-8")
    assert "sk-super-secret" not in raw


# ------------------------------------------------------------- the API key


def test_no_key_ever_saved_loads_as_empty_string(tmp_path):
    assert ai_settings.load_api_key(base_dir=tmp_path) == ""
    assert ai_settings.api_key_is_stored(base_dir=tmp_path) is False


def test_save_and_load_api_key_round_trip(tmp_path):
    ai_settings.save_api_key("sk-ant-abc123", base_dir=tmp_path)
    assert ai_settings.load_api_key(base_dir=tmp_path) == "sk-ant-abc123"
    assert ai_settings.api_key_is_stored(base_dir=tmp_path) is True


def test_the_api_key_is_not_stored_in_plain_text_on_disk(tmp_path):
    ai_settings.save_api_key("sk-ant-abc123", base_dir=tmp_path)
    on_disk = (tmp_path / "ai_api_key.enc").read_bytes()
    assert b"sk-ant-abc123" not in on_disk


def test_saving_an_empty_key_clears_any_stored_key(tmp_path):
    ai_settings.save_api_key("sk-ant-abc123", base_dir=tmp_path)
    assert ai_settings.api_key_is_stored(base_dir=tmp_path) is True
    ai_settings.save_api_key("", base_dir=tmp_path)
    assert ai_settings.api_key_is_stored(base_dir=tmp_path) is False
    assert ai_settings.load_api_key(base_dir=tmp_path) == ""


def test_a_corrupted_key_file_loads_as_empty_string_rather_than_raising(tmp_path):
    ai_settings.save_api_key("sk-ant-abc123", base_dir=tmp_path)
    (tmp_path / "ai_api_key.enc").write_bytes(b"not a valid fernet token")
    assert ai_settings.load_api_key(base_dir=tmp_path) == ""


def test_a_missing_secret_key_file_but_present_encrypted_file_loads_as_empty(tmp_path):
    ai_settings.save_api_key("sk-ant-abc123", base_dir=tmp_path)
    (tmp_path / "ai_secret.key").unlink()
    # A fresh key gets generated, but it can't decrypt a token written
    # under the old (now-gone) key -- this must fail closed, not raise.
    assert ai_settings.load_api_key(base_dir=tmp_path) == ""


def test_two_different_base_dirs_do_not_share_a_key(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    ai_settings.save_api_key("secret-a", base_dir=dir_a)
    ai_settings.save_api_key("secret-b", base_dir=dir_b)
    assert ai_settings.load_api_key(base_dir=dir_a) == "secret-a"
    assert ai_settings.load_api_key(base_dir=dir_b) == "secret-b"


# ------------------------------------------------------------ to_ai_config


def test_to_ai_config_combines_settings_and_the_stored_key(tmp_path):
    ai_settings.save_api_key("sk-combined", base_dir=tmp_path)
    settings = ai_settings.AiSettings(
        enabled=True, provider="openai", model="gpt-4o", feature_nl_config=False)
    cfg = ai_settings.to_ai_config(settings, base_dir=tmp_path)
    assert cfg.enabled is True
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"
    assert cfg.api_key == "sk-combined"
    assert cfg.feature_nl_config is False


def test_to_ai_config_with_no_stored_key_gives_an_empty_api_key(tmp_path):
    cfg = ai_settings.to_ai_config(ai_settings.AiSettings(enabled=True), base_dir=tmp_path)
    assert cfg.api_key == ""
