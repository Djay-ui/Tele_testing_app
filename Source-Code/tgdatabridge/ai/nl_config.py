"""Turn a free-text migration request ("migrate all customer tables from
Oracle to Postgres, skip anything archived") into a structured selection
this tool's existing controls can act on.

Deliberately advisory, not autonomous: this module never runs a migration,
never selects checkboxes in the GUI itself, and never writes a CLI job
config file. It returns an NlConfigResult for a human to read and then act
on through the tool's ordinary controls (the schema tree's checkboxes, the
CLI's own config file) -- the same reasoning as schema_mapper.py staying a
review layer rather than an autonomous decision-maker: a free-text request
is inherently ambiguous ("customer tables" could mean anything), and this
tool moves real data, so the one thing this module must never do is
silently guess wrong and run with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from tgdatabridge.ai.ai_client import AiClient, AiError

_SYSTEM_PROMPT_TEMPLATE = (
    "You translate a plain-English database migration request into a "
    "structured selection. You are given the exact list of table names "
    "that actually exist in this migration's source schema -- you may "
    "only select from that list, never invent a table name. Reply with "
    "ONLY a JSON object, no prose before or after it, no markdown fence: "
    '{{"tables": [<table names from the given list that match the '
    'request>], "filters": {{<table name>: <a plain-English description '
    "of a row filter the user asked for, if any -- e.g. \"skip rows where "
    'status = archived\"}}, "notes": <a short plain-English summary of '
    "what you selected and why, or anything in the request you could not "
    'confidently map to a table>}}. If the request does not clearly match '
    'any given table, return an empty "tables" list and explain why in '
    '"notes" rather than guessing. The available tables are:\n{table_list}'
)


@dataclass
class NlConfigResult:
    tables: List[str] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    notes: str = ""


def parse_migration_request(client: AiClient, request_text: str,
                             available_tables: List[str]) -> NlConfigResult:
    """Ask the configured AI backend to map `request_text` onto a subset
    of `available_tables` (and optional per-table filter descriptions).
    Raises AiError -- unchanged -- for anything that goes wrong, including
    an empty `request_text` (there is nothing to parse) or an empty
    `available_tables` (nothing to select from, most likely because the
    source schema hasn't been loaded yet -- see the GUI's AI Review
    dialog, which checks for this before ever building a client)."""
    if not request_text.strip():
        raise AiError("Type what you want to migrate first.")
    if not available_tables:
        raise AiError("Load the source schema first (step 1) -- there is nothing to select from yet.")

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        table_list="\n".join(f"  - {t}" for t in available_tables))
    raw = client.complete_json(system_prompt, request_text.strip())
    if not isinstance(raw, dict):
        raise AiError("The AI's response did not come back as an object as requested.")

    known = set(available_tables)
    raw_tables = raw.get("tables")
    tables = [t for t in raw_tables if isinstance(t, str) and t in known] if isinstance(raw_tables, list) else []

    raw_filters = raw.get("filters")
    filters = {
        k: str(v) for k, v in raw_filters.items() if k in known
    } if isinstance(raw_filters, dict) else {}

    return NlConfigResult(tables=tables, filters=filters, notes=str(raw.get("notes") or ""))


__all__ = ["NlConfigResult", "parse_migration_request"]
