"""
Rolls up per-object conversion status into a schema-level migration
assessment summary, mirroring the kind of "action items" summary AWS SCT
shows before you commit a conversion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from tgdatabridge.core.schema_model import ConversionStatus, Schema


@dataclass
class AssessmentSummary:
    schema_name: str
    source_engine: str
    target_engine: str

    counts_by_status: Dict[str, int] = field(default_factory=dict)
    counts_by_type: Dict[str, int] = field(default_factory=dict)
    total_objects: int = 0
    automatic_pct: float = 0.0
    estimated_manual_hours: float = 0.0
    action_items: List[str] = field(default_factory=list)


# rough per-object manual-effort estimate in hours, used only to give a
# ballpark sizing figure the way SCT's assessment report does
_EFFORT_HOURS = {
    "table": 0.25,
    "view": 0.5,
    "sequence": 0.1,
    "routine_low": 1.0,
    "routine_high": 4.0,
}


def build_assessment(schema: Schema) -> AssessmentSummary:
    summary = AssessmentSummary(
        schema_name=schema.name,
        source_engine=schema.source_engine,
        target_engine=schema.target_engine,
    )

    counts_by_status: Dict[str, int] = {}
    counts_by_type = {
        "Tables": len(schema.tables),
        "Views": len(schema.views),
        "Sequences": len(schema.sequences),
        "Stored routines / triggers": len(schema.routines),
    }

    def tally(status: ConversionStatus):
        counts_by_status[status.value] = counts_by_status.get(status.value, 0) + 1

    hours = 0.0
    for t in schema.tables:
        tally(t.status)
        if t.status != ConversionStatus.AUTOMATIC:
            hours += _EFFORT_HOURS["table"]
    for v in schema.views:
        tally(v.status)
        if v.status != ConversionStatus.AUTOMATIC:
            hours += _EFFORT_HOURS["view"]
    for s in schema.sequences:
        tally(s.status)
        if s.status != ConversionStatus.AUTOMATIC:
            hours += _EFFORT_HOURS["sequence"]
    for r in schema.routines:
        tally(r.status)
        if r.status == ConversionStatus.MANUAL:
            hours += (_EFFORT_HOURS["routine_high"] if r.complexity_score > 4
                      else _EFFORT_HOURS["routine_low"])

    total = sum(counts_by_type.values())
    automatic = counts_by_status.get(ConversionStatus.AUTOMATIC.value, 0)

    summary.counts_by_status = counts_by_status
    summary.counts_by_type = counts_by_type
    summary.total_objects = total
    summary.automatic_pct = round(100.0 * automatic / total, 1) if total else 0.0
    summary.estimated_manual_hours = round(hours, 1)

    action_items: List[str] = []
    for t in schema.tables:
        if t.status != ConversionStatus.AUTOMATIC:
            action_items.append(f"Table {t.name}: review {len(t.issues)} issue(s) before deployment.")
    for v in schema.views:
        if v.status != ConversionStatus.AUTOMATIC:
            action_items.append(f"View {v.name}: rewrite Oracle-specific SQL constructs.")
    for s in schema.sequences:
        if s.status != ConversionStatus.AUTOMATIC:
            action_items.append(f"Sequence {s.name}: verify emulation strategy on target.")
    for r in schema.routines:
        if r.status == ConversionStatus.MANUAL:
            action_items.append(
                f"{r.kind.title()} {r.name}: manual conversion required (complexity score {r.complexity_score})."
            )

    summary.action_items = action_items
    return summary
