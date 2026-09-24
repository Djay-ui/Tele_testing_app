"""
Best-effort PL/SQL -> target-dialect translation for stored procedures,
functions, packages, and triggers.

Full PL/SQL -> PL/pgSQL / MySQL stored-procedure translation is not a
solved problem in general (this is the same reality AWS SCT documents:
it auto-converts a meaningful fraction of routines and flags the rest as
"requires manual conversion" with an action item). This module applies
a set of safe, mechanical textual substitutions for common constructs,
computes a complexity score from how many Oracle-specific constructs
remain, and always leaves the routine flagged for manual review above a
low complexity threshold so nothing is silently mis-migrated.
"""
from __future__ import annotations

import re
from typing import List

from tgdatabridge.core.schema_model import ConversionIssue, ConversionStatus, Routine

# constructs that raise complexity score, with a fixed weight and message
_COMPLEXITY_MARKERS = [
    (re.compile(r"\bDBMS_[A-Z_]+", re.IGNORECASE), 3, "Uses an Oracle DBMS_* package with no direct equivalent"),
    (re.compile(r"\bUTL_[A-Z_]+", re.IGNORECASE), 3, "Uses an Oracle UTL_* package with no direct equivalent"),
    (re.compile(r"%ROWTYPE", re.IGNORECASE), 1, "Uses %ROWTYPE anchored typing"),
    (re.compile(r"%TYPE", re.IGNORECASE), 1, "Uses %TYPE anchored typing"),
    (re.compile(r"\bPRAGMA\b", re.IGNORECASE), 2, "Uses a PRAGMA directive"),
    (re.compile(r"\bBULK COLLECT\b", re.IGNORECASE), 2, "Uses BULK COLLECT bulk-fetch"),
    (re.compile(r"\bFORALL\b", re.IGNORECASE), 2, "Uses FORALL bulk-DML"),
    (re.compile(r"\bAUTONOMOUS_TRANSACTION\b", re.IGNORECASE), 3, "Uses an autonomous transaction pragma"),
    (re.compile(r"\bCONNECT BY\b", re.IGNORECASE), 3, "Uses hierarchical CONNECT BY query"),
    (re.compile(r"\bMERGE\b", re.IGNORECASE), 1, "Uses a MERGE statement (syntax differs by target)"),
    (re.compile(r"\bCURSOR\b", re.IGNORECASE), 1, "Declares an explicit cursor"),
    (re.compile(r"\bEXCEPTION\b", re.IGNORECASE), 1, "Has custom exception handling"),
    (re.compile(r"\bPACKAGE\b", re.IGNORECASE), 2, "Is part of a PACKAGE (target engines have no package concept)"),
    (re.compile(r"\bGOTO\b", re.IGNORECASE), 1, "Uses GOTO control flow"),
]

# safe mechanical substitutions applied to produce a "converted_source" draft
_SUBSTITUTIONS = [
    (re.compile(r"\bSYSDATE\b", re.IGNORECASE), "CURRENT_TIMESTAMP"),
    (re.compile(r"\bNVL\s*\(", re.IGNORECASE), "COALESCE("),
    (re.compile(r"\|\|", re.IGNORECASE), "||"),  # concatenation is portable to Postgres, kept as-is
]


def score_complexity(source: str) -> tuple[int, List[ConversionIssue]]:
    score = 0
    issues: List[ConversionIssue] = []
    for pattern, weight, message in _COMPLEXITY_MARKERS:
        matches = pattern.findall(source)
        if matches:
            score += weight * len(matches)
            issues.append(ConversionIssue("warning", f"{message} ({len(matches)}x)."))
    return score, issues


def translate_routine(routine: Routine, target_engine: str) -> Routine:
    score, issues = score_complexity(routine.source)
    routine.complexity_score = score

    draft = routine.source
    for pattern, replacement in _SUBSTITUTIONS:
        draft = pattern.sub(replacement, draft)
    routine.converted_source = draft

    if score == 0:
        routine.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
        issues.append(ConversionIssue(
            "info",
            "No high-risk Oracle-specific constructs detected. A draft conversion was generated, "
            "but stored routines should still be reviewed and tested against the target engine "
            f"({target_engine}) before use.",
        ))
    elif score <= 4:
        routine.status = ConversionStatus.MANUAL
        issues.append(ConversionIssue(
            "warning", "Low-to-moderate complexity; manual review recommended before deployment.",
        ))
    else:
        routine.status = ConversionStatus.MANUAL
        issues.append(ConversionIssue(
            "error", "High complexity; this routine requires hand conversion by a developer.",
        ))

    routine.issues = issues
    return routine


def translate_all_routines(routines: List[Routine], target_engine: str) -> List[Routine]:
    return [translate_routine(r, target_engine) for r in routines]
