"""Plain-language explanation and fix suggestions for an error this tool
hit -- a connection failure, a failed DDL statement, a migration exception.

This module does not read or transmit any migrated *data* -- only the
error text itself and a short, caller-supplied context string (e.g. "Apply
DDL to Target, PostgreSQL", never a connection string, password, or any
row of actual data). Every call site in the GUI/CLI is responsible for
building that context string itself; this module trusts what it's given
and does no filtering of its own, so callers should not pass anything they
would not be comfortable leaving this tool's process.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from tgdatabridge.ai.ai_client import AiClient, AiError

_SYSTEM_PROMPT = (
    "You are a senior database migration engineer helping a colleague "
    "understand an error from a schema/data migration tool that moves "
    "data between Oracle, MySQL, PostgreSQL, SQL Server, Db2, MongoDB and "
    "Excel/CSV. Explain what the error means in plain language a "
    "non-specialist could follow, then give concrete, actionable fix "
    "suggestions specific to this error -- not generic troubleshooting "
    "advice. Reply with ONLY a JSON object, no prose before or after it, "
    'no markdown fence: {"explanation": <2-4 sentences, plain language>, '
    '"suggested_fixes": [<short, specific, actionable step>, ...]}. '
    "Give at most 5 suggested fixes, ordered most-likely-to-help first."
)


@dataclass
class Diagnosis:
    explanation: str = ""
    suggested_fixes: List[str] = field(default_factory=list)


def explain_error(client: AiClient, error_text: str, context: str = "") -> Diagnosis:
    """Ask the configured AI backend to explain `error_text` (an exception
    message, a driver error, a short log excerpt) and suggest fixes.
    `context` is an optional short label for what was happening (e.g.
    "Migrate Data, MySQL -> PostgreSQL") -- purely to help the model give a
    more specific answer, never required. Raises AiError -- unchanged --
    for anything that goes wrong, including an empty `error_text`."""
    if not error_text.strip():
        raise AiError("There is no error text to explain.")

    user_prompt = error_text.strip()
    if context.strip():
        user_prompt = f"Context: {context.strip()}\n\nError:\n{user_prompt}"

    raw = client.complete_json(_SYSTEM_PROMPT, user_prompt)
    if not isinstance(raw, dict):
        raise AiError("The AI's response did not come back as an object as requested.")

    fixes_raw = raw.get("suggested_fixes")
    fixes = [str(f) for f in fixes_raw if isinstance(f, str) and f.strip()] if isinstance(fixes_raw, list) else []
    return Diagnosis(explanation=str(raw.get("explanation") or "").strip(), suggested_fixes=fixes)


__all__ = ["Diagnosis", "explain_error"]
