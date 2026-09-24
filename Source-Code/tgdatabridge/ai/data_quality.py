"""Post-migration data quality review: a small sample of migrated rows,
plus source/target row counts, sent to the configured AI backend to flag
likely problems -- unexpected nulls, an encoding artifact, a suspicious
outlier, a row-count mismatch that validation's own checksum comparison
(tgdatabridge.core.validation) wouldn't characterize in words.

This is advisory only and runs on a *sample*, never the whole table --
tgdatabridge.core.validation's row-count and checksum comparison is still
the authoritative correctness check for a migration; this module exists to
catch the kind of problem that is technically "correct" (every row copied,
checksums match) but still wrong in a way a human reviewer would notice at
a glance, like a column that migrated as all NULLs because a rule-based
type conversion silently couldn't parse the source values.

**This is the one AI feature in this tool that transmits actual migrated
data** to whichever provider AiConfig points at -- schema/column names and
error text in the other three features, but here, real row values. Every
call site must cap the sample it passes in (`MAX_SAMPLE_ROWS` below is
enforced here too, defensively, but callers should never rely on that
enforcement instead of sampling deliberately) and this tool's AI Settings
screen says so plainly; a shop with data-residency or confidentiality
requirements should leave `feature_data_quality` off and use the other
three features, or point AiConfig at a "compatible" backend running fully
inside their own network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Sequence

from tgdatabridge.ai.ai_client import AiClient, AiError

MAX_SAMPLE_ROWS = 25
_VALID_SEVERITIES = ("info", "warning", "critical")

_SYSTEM_PROMPT = (
    "You are reviewing a small sample of rows that were just migrated "
    "from one database to another, to spot likely data quality problems "
    "the migration tool's own row-count and checksum validation would not "
    "describe in words -- for example a column that is unexpectedly all "
    "NULL or empty, a garbled/mis-encoded string, a date that looks "
    "shifted, a numeric value that looks truncated or overflowed, or a "
    "suspicious outlier. Do not flag things that are merely unusual but "
    "plausible (e.g. a legitimately rare value) -- only flag what looks "
    "like an actual migration defect. If nothing looks wrong, say so. "
    "Reply with ONLY a JSON array, no prose before or after it, no "
    'markdown fence. Each element: {"column": <column name, or "" if the '
    'finding is about the table as a whole>, "severity": '
    '"info"|"warning"|"critical", "description": <what you noticed and '
    'why it looks wrong>}.'
)


@dataclass
class QualityFinding:
    column: str
    severity: str
    description: str


def review_sample(client: AiClient, table_name: str, columns: Sequence[str],
                   sample_rows: Sequence[Sequence[Any]],
                   source_row_count: int, target_row_count: int) -> List[QualityFinding]:
    """Ask the configured AI backend to review `sample_rows` (each a
    sequence of values aligned to `columns`) from `table_name`, alongside
    the source/target row counts. `sample_rows` is truncated to
    MAX_SAMPLE_ROWS here defensively; callers should already be sampling,
    not passing a whole table (see the module docstring). Raises AiError
    -- unchanged -- for anything that goes wrong, including an empty
    sample (there is nothing to review)."""
    if not columns or not sample_rows:
        raise AiError("There is no sample data to review -- migrate this table first.")

    capped = list(sample_rows)[:MAX_SAMPLE_ROWS]

    lines = [
        f"Table: {table_name}",
        f"Source row count: {source_row_count}",
        f"Target row count: {target_row_count}",
        f"Columns: {', '.join(columns)}",
        f"Sample ({len(capped)} of {target_row_count} target rows):",
    ]
    for row in capped:
        cells = ", ".join(f"{col}={_render_cell(val)}" for col, val in zip(columns, row))
        lines.append(f"  - {{{cells}}}")
    user_prompt = "\n".join(lines)

    raw = client.complete_json(_SYSTEM_PROMPT, user_prompt)
    if not isinstance(raw, list):
        raise AiError("The AI's data quality review did not come back as a list as requested.")

    known_columns = set(columns) | {""}
    findings: List[QualityFinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        column = item.get("column") if item.get("column") in known_columns else ""
        severity = item.get("severity") if item.get("severity") in _VALID_SEVERITIES else "info"
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        findings.append(QualityFinding(column=column, severity=severity, description=description))
    return findings


def _render_cell(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    # A single cell running to thousands of characters (a large CLOB/BLOB
    # sample, say) would blow the sample's usefulness and its token cost
    # for no benefit -- the model only needs enough of it to judge
    # plausibility, not the whole thing.
    return text if len(text) <= 200 else text[:200] + "…"


__all__ = ["QualityFinding", "MAX_SAMPLE_ROWS", "review_sample"]
