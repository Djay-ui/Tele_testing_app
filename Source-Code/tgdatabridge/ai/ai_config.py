"""Configuration for the optional AI-assisted features (schema/column
mapping review, natural-language job requests, error diagnosis, and
post-migration data quality review) -- see tgdatabridge/ai/ai_client.py for
what actually calls out to a provider, and tgdatabridge/utils/ai_settings.py
for how this is persisted between runs.

Deliberately provider-agnostic, on the same principle as
tgdatabridge/db/tls_config.py being connector-agnostic: this dataclass
describes *what* AI backend to use and *how*, not any one vendor's SDK.
That matters here for a reason TlsConfig didn't have to worry about --
`repack.py` (see repack/README or WHATS-FIXED.md) rebuilds this tool's own
bytecode without touching the frozen `_internal` folder of third-party
packages. A vendor SDK (the `anthropic` or `openai` PyPI package) is not in
that folder and adding one means a full rebuild this tool cannot currently
ship as an incremental patch. `tgdatabridge/ai/ai_client.py` therefore
speaks plain HTTPS via the standard library's own `urllib.request` to each
provider's REST API directly -- no SDK, nothing new to bundle.

Four providers are supported, chosen once per installation:

  "claude"         Anthropic's Messages API (api.anthropic.com by default).
  "openai"         OpenAI's Chat Completions API (api.openai.com).
  "azure_openai"   Azure OpenAI Service -- an enterprise's own Azure
                    deployment of an OpenAI model. Always needs base_url
                    (the resource endpoint) since there is no shared
                    default the way api.openai.com is one.
  "compatible"     Any other server that speaks the OpenAI Chat Completions
                    wire format -- this is how a fully offline/local model
                    plugs in (Ollama's OpenAI-compatible endpoint, LM
                    Studio, vLLM, a company's own internal gateway), so
                    that "no data leaves our network" is achievable without
                    this tool having to know about any specific local
                    runtime. Always needs base_url; api_key is often blank
                    for a local server that doesn't check one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PROVIDERS = ("claude", "openai", "azure_openai", "compatible")

# Providers with a well-known public endpoint -- base_url is optional for
# these (blank uses the default below) and required for the other two.
_DEFAULT_BASE_URLS = {
    "claude": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}
_PROVIDERS_REQUIRING_BASE_URL = ("azure_openai", "compatible")

# A reasonable off-the-shelf default per provider, used only when `model`
# is left blank. An enterprise Azure deployment names its own deployment as
# the "model" (Azure's deployment name stands in for the model name in the
# URL -- see ai_client.py), so azure_openai has no sane default at all and
# is deliberately absent from this dict; validate() requires it explicitly.
_DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
}

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TOKENS = 1500


@dataclass
class AiConfig:
    """One installation's AI backend settings, plus which of the four
    features are switched on. `enabled=False` (the default) means the
    tool behaves exactly as it did before this feature existed -- nothing
    here is on a hot path anywhere else in the tool; every call site
    checks `enabled` (and the specific `feature_*` flag) before ever
    constructing an AiClient.
    """

    enabled: bool = False
    provider: str = "claude"

    # Never logged, never included in describe() or any exception message
    # this module raises -- see ai_client.py's own note on this. Persisted
    # encrypted-at-rest, separately from every other field here; see
    # tgdatabridge/utils/ai_settings.py.
    api_key: str = ""

    # Required for "azure_openai" and "compatible" (there's no shared
    # default endpoint for either -- an Azure resource URL is unique per
    # customer, and a local/offline server's address is unique per
    # machine). Optional for "claude"/"openai": blank uses the provider's
    # public API.
    base_url: str = ""

    # Blank uses _DEFAULT_MODELS' entry for "claude"/"openai". Required for
    # "azure_openai" (this is the *deployment name*, not a model family --
    # see the module docstring) and for "compatible" (a local server has no
    # universal default to assume).
    model: str = ""

    # Azure OpenAI versions its REST API independently of the model itself;
    # only meaningful when provider == "azure_openai".
    azure_api_version: str = "2024-06-01"

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    # Each of the four features is independently switchable -- a shop that
    # only wants AI-assisted error messages (say, because they are not
    # comfortable sending table/column names to a third-party API for
    # mapping review) can turn on exactly that one and leave the rest off,
    # without disabling the feature entirely.
    feature_schema_mapping: bool = True
    feature_nl_config: bool = True
    feature_error_diagnostics: bool = True
    feature_data_quality: bool = True

    def validate(self) -> Optional[str]:
        """A human-readable reason this configuration cannot be used, or
        None. Checked before an AiClient will make any request -- the same
        contract as TlsConfig.validate() and SshTunnelConfig.validate()."""
        if not self.enabled:
            return None
        if self.provider not in PROVIDERS:
            return (f'Unknown AI provider "{self.provider}" -- choose one of: '
                    f'{", ".join(PROVIDERS)}.')
        if not self.api_key and self.provider != "compatible":
            return ("An API key is required for this provider. A local/offline "
                    "server (\"compatible\") is the only one that can be used "
                    "with no key at all.")
        if self.provider in _PROVIDERS_REQUIRING_BASE_URL and not self.base_url.strip():
            which = "Azure OpenAI" if self.provider == "azure_openai" else "a local/offline server"
            return (f'"Base URL" is required for {which} -- there is no shared '
                    f"public default the way there is for Claude or OpenAI.")
        if self.provider == "azure_openai" and not self.model.strip():
            return ('"Model" is required for Azure OpenAI -- this is the name of '
                    "your own deployment, which Azure has no way to default.")
        if self.timeout_seconds <= 0:
            return '"Timeout" must be a positive number of seconds.'
        return None

    def describe(self) -> str:
        """One line for a log or a status message. Never includes the API
        key."""
        if not self.enabled:
            return "AI features off"
        label = {
            "claude": "Claude", "openai": "OpenAI",
            "azure_openai": "Azure OpenAI", "compatible": "a local/offline server",
        }.get(self.provider, self.provider)
        model = self.effective_model()
        on = [name for name, flag in (
            ("mapping review", self.feature_schema_mapping),
            ("plain-English requests", self.feature_nl_config),
            ("error diagnosis", self.feature_error_diagnostics),
            ("data quality review", self.feature_data_quality),
        ) if flag]
        features = ", ".join(on) if on else "no features turned on"
        return f"AI via {label} ({model}) -- {features}"

    def effective_base_url(self) -> str:
        """The URL to call, after applying the provider's default (for
        "claude"/"openai" with nothing overridden). Empty for
        "azure_openai"/"compatible" left unconfigured -- callers should
        have already rejected that via validate() before reaching here."""
        if self.base_url.strip():
            return self.base_url.strip().rstrip("/")
        return _DEFAULT_BASE_URLS.get(self.provider, "")

    def effective_model(self) -> str:
        """The model/deployment name to use, after applying the
        provider's default where one exists."""
        if self.model.strip():
            return self.model.strip()
        return _DEFAULT_MODELS.get(self.provider, "")


__all__ = ["AiConfig", "PROVIDERS", "DEFAULT_MAX_TOKENS"]
