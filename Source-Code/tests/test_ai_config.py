"""Tests for tgdatabridge.ai.ai_config.AiConfig."""
from tgdatabridge.ai.ai_config import AiConfig


def test_disabled_needs_no_validation():
    assert AiConfig(enabled=False).validate() is None
    assert AiConfig(enabled=False, provider="nonsense").validate() is None


def test_a_default_enabled_claude_config_with_a_key_is_valid():
    assert AiConfig(enabled=True, api_key="sk-ant-xyz").validate() is None


def test_an_unknown_provider_is_rejected():
    problem = AiConfig(enabled=True, provider="bogus", api_key="k").validate()
    assert problem is not None
    assert "bogus" in problem


def test_a_missing_api_key_is_rejected_for_claude():
    problem = AiConfig(enabled=True, provider="claude", api_key="").validate()
    assert problem is not None
    assert "API key" in problem


def test_compatible_provider_may_omit_the_api_key():
    cfg = AiConfig(enabled=True, provider="compatible", api_key="", base_url="http://localhost:11434")
    assert cfg.validate() is None


def test_azure_openai_requires_a_base_url():
    problem = AiConfig(enabled=True, provider="azure_openai", api_key="k", model="gpt-4").validate()
    assert problem is not None
    assert "Base URL" in problem


def test_compatible_requires_a_base_url():
    problem = AiConfig(enabled=True, provider="compatible", api_key="k").validate()
    assert problem is not None
    assert "Base URL" in problem


def test_azure_openai_requires_a_model_deployment_name():
    problem = AiConfig(
        enabled=True, provider="azure_openai", api_key="k",
        base_url="https://my-resource.openai.azure.com").validate()
    assert problem is not None
    assert "Model" in problem


def test_a_fully_configured_azure_openai_is_valid():
    cfg = AiConfig(
        enabled=True, provider="azure_openai", api_key="k",
        base_url="https://my-resource.openai.azure.com", model="my-deployment")
    assert cfg.validate() is None


def test_a_non_positive_timeout_is_rejected():
    problem = AiConfig(enabled=True, api_key="k", timeout_seconds=0).validate()
    assert problem is not None
    assert "Timeout" in problem


def test_describe_never_includes_the_api_key():
    cfg = AiConfig(enabled=True, api_key="super-secret-value", provider="claude")
    assert "super-secret-value" not in cfg.describe()


def test_describe_when_disabled():
    assert AiConfig(enabled=False).describe() == "AI features off"


def test_describe_lists_enabled_features_only():
    cfg = AiConfig(enabled=True, api_key="k", feature_schema_mapping=True,
                    feature_nl_config=False, feature_error_diagnostics=False,
                    feature_data_quality=False)
    description = cfg.describe()
    assert "mapping review" in description
    assert "error diagnosis" not in description


def test_describe_with_no_features_on():
    cfg = AiConfig(enabled=True, api_key="k", feature_schema_mapping=False,
                    feature_nl_config=False, feature_error_diagnostics=False,
                    feature_data_quality=False)
    assert "no features turned on" in cfg.describe()


def test_effective_base_url_uses_the_provider_default_when_blank():
    assert AiConfig(provider="claude").effective_base_url() == "https://api.anthropic.com"
    assert AiConfig(provider="openai").effective_base_url() == "https://api.openai.com"


def test_effective_base_url_strips_a_trailing_slash():
    cfg = AiConfig(provider="compatible", base_url="http://localhost:11434/")
    assert cfg.effective_base_url() == "http://localhost:11434"


def test_effective_base_url_is_empty_for_unconfigured_azure():
    assert AiConfig(provider="azure_openai").effective_base_url() == ""


def test_effective_model_uses_the_provider_default_when_blank():
    assert AiConfig(provider="claude").effective_model() == "claude-sonnet-4-5"
    assert AiConfig(provider="openai").effective_model() == "gpt-4o-mini"


def test_effective_model_prefers_an_explicit_value():
    cfg = AiConfig(provider="claude", model="claude-opus-4")
    assert cfg.effective_model() == "claude-opus-4"


def test_effective_model_is_empty_for_unconfigured_azure_or_compatible():
    assert AiConfig(provider="azure_openai").effective_model() == ""
    assert AiConfig(provider="compatible").effective_model() == ""
