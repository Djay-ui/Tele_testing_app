"""A thin, dependency-free client for the four AI backends AiConfig can
point at -- see that module's docstring for why this speaks raw HTTPS via
`urllib.request` (standard library only) instead of a vendor SDK.

Every feature module in this package (schema_mapper, nl_config,
error_diagnostics, data_quality) is written against this class's two
methods and never touches urllib or a provider's wire format directly, so
adding a fifth provider later is a change to this one file.

Testing note: the actual HTTP call is made through the injectable
`transport` callable (default `_urllib_transport`), exactly the seam
tgdatabridge.utils.app_storage's `base_dir` parameter provides for the
filesystem -- tests supply a fake transport and never touch the network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from tgdatabridge.ai.ai_config import AiConfig, DEFAULT_MAX_TOKENS

# (request, timeout_seconds) -> raw response bytes. Raises on transport
# failure (connection refused, DNS failure, timeout, TLS error, or an
# HTTP error status -- urllib.request.urlopen raises HTTPError, itself a
# URLError, for any non-2xx response).
Transport = Callable[[urllib.request.Request, float], bytes]


class AiError(Exception):
    """Anything that stops an AI call from producing a usable answer: a
    bad configuration, an unreachable service, an HTTP error, or a
    response this module could not parse. Always a plain, user-showable
    message -- every raise site in this module writes one assuming it
    will end up verbatim in a GUI dialog or a CLI log line."""


def _urllib_transport(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class AiClient:
    """Sends one prompt, gets one answer -- no conversation state, no
    streaming, no tool use. Every feature in this package is a single
    request/response round trip by design: each is a discrete "look at
    this and tell me" question, not a chat."""

    def __init__(self, config: AiConfig, transport: Optional[Transport] = None):
        self.config = config
        self._transport = transport or _urllib_transport

    def complete(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        """Ask the configured backend to answer `user_prompt` (with
        `system_prompt` steering how), and return its plain-text reply.
        Raises AiError for anything that goes wrong -- a bad config, an
        unreachable/erroring service, or a response shaped in a way this
        client doesn't recognise."""
        problem = self.config.validate()
        if problem:
            raise AiError(problem)

        request = self._build_request(system_prompt, user_prompt, max_tokens)
        try:
            raw = self._transport(request, self.config.timeout_seconds)
        except urllib.error.HTTPError as exc:
            body = _safe_read_error_body(exc)
            raise AiError(
                f"The AI service rejected the request (HTTP {exc.code}): "
                f"{body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise AiError(f"Could not reach the AI service: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AiError(
                f"The AI service did not respond within "
                f"{self.config.timeout_seconds:g} seconds.") from exc
        except OSError as exc:
            raise AiError(f"Could not reach the AI service: {exc}") from exc

        return self._parse_response(raw)

    def complete_json(self, system_prompt: str, user_prompt: str,
                       max_tokens: int = DEFAULT_MAX_TOKENS) -> Any:
        """Like complete(), but parses the reply as JSON and raises
        AiError (rather than returning malformed data) if it isn't
        valid JSON -- every feature module in this package asks for a
        strict JSON shape in its own prompt and depends on this to fail
        loudly instead of handing back something it can't trust."""
        text = self.complete(system_prompt, user_prompt, max_tokens=max_tokens)
        candidate = _strip_markdown_fence(text)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AiError(
                "The AI did not return valid JSON. This can happen "
                "occasionally with any model -- try again.") from exc

    # ---------------------------------------------------------- requests

    def _build_request(self, system_prompt: str, user_prompt: str,
                        max_tokens: int) -> urllib.request.Request:
        provider = self.config.provider
        if provider == "claude":
            return self._claude_request(system_prompt, user_prompt, max_tokens)
        if provider in ("openai", "azure_openai", "compatible"):
            return self._openai_like_request(system_prompt, user_prompt, max_tokens)
        raise AiError(f'Unknown AI provider "{provider}".')

    def _claude_request(self, system_prompt: str, user_prompt: str,
                         max_tokens: int) -> urllib.request.Request:
        url = f"{self.config.effective_base_url()}/v1/messages"
        payload = {
            "model": self.config.effective_model(),
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }
        return urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

    def _openai_like_request(self, system_prompt: str, user_prompt: str,
                              max_tokens: int) -> urllib.request.Request:
        # OpenAI, Azure OpenAI and any OpenAI-compatible local server (see
        # the module docstring) all speak the same Chat Completions body --
        # only the URL and auth header differ.
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}

        if self.config.provider == "azure_openai":
            deployment = self.config.effective_model()
            url = (f"{self.config.effective_base_url()}/openai/deployments/"
                   f"{deployment}/chat/completions"
                   f"?api-version={self.config.azure_api_version}")
            headers["api-key"] = self.config.api_key
        else:
            url = f"{self.config.effective_base_url()}/v1/chat/completions"
            payload["model"] = self.config.effective_model()
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

        return urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

    # --------------------------------------------------------- responses

    def _parse_response(self, raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AiError("The AI service returned a response this tool could not "
                           "understand.") from exc

        try:
            if self.config.provider == "claude":
                return "".join(
                    block.get("text", "") for block in data["content"]
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
            # openai / azure_openai / compatible
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise AiError("The AI service returned a response this tool could not "
                           "understand (unexpected shape).") from exc


def _safe_read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
        data = json.loads(raw.decode("utf-8"))
        # Both OpenAI-shaped and Claude-shaped error bodies nest the
        # message under an "error" object; fall back to the raw text for
        # anything else (a plain-text 502 from a proxy, for instance).
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
        return raw.decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001
        return ""


def _strip_markdown_fence(text: str) -> str:
    """Models asked for JSON frequently wrap it in a ```json ... ```
    fence anyway. Strip one if present; leave the text alone otherwise."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        lines = lines[1:-1]
    else:
        lines = lines[1:]
    return "\n".join(lines).strip()


__all__ = ["AiClient", "AiError"]
