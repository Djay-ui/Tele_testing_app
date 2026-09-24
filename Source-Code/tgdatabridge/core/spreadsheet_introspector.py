"""Infers a tgdatabridge.core.schema_model.Schema from an Excel workbook or a
CSV/TSV file, for using a spreadsheet as a *source* -- e.g. loading
reference data, lookup tables, or legacy data that only ever lived in a
spreadsheet into PostgreSQL/MySQL/SQL Server/Db2/Oracle/MongoDB.

What this is honest about
-------------------------
A spreadsheet is not a database, and pretending otherwise would produce
a worse migration than admitting it. There are no primary keys, no
foreign keys, no indexes, no views, no stored routines and no sequences
in an .xlsx file, so none are inferred here -- `schema.views`,
`.sequences` and `.routines` are always empty, and no Constraint or Index
is ever synthesized. (Compare mongo_source_introspector, which *does*
synthesize a PK and FKs, because MongoDB genuinely guarantees a unique
`_id` and array normalization genuinely creates a parent/child
relationship. Neither is true of a worksheet: a column that looks like an
ID has no uniqueness guarantee whatsoever, and inventing a PRIMARY KEY
constraint from that guess would make Apply DDL fail on the target the
moment a duplicate showed up.)

The consequence is that the assessment report for a spreadsheet source is
sparse -- there is nothing to assess. The value of this source engine is
in the *data migration* half of the tool, not the schema-conversion half.

The shape it expects
--------------------
One sheet = one table. Row 1 = the header. Data starts at row 2. A file
that doesn't match that gets a clear error or a warning rather than a
best-effort guess:

  - a sheet with no rows at all is skipped with a warning issue;
  - a header cell that's blank becomes `column_N` with a warning;
  - duplicate header names get a numeric suffix with a warning;
  - a sheet name or header that isn't a legal SQL identifier is sanitized
    (see spreadsheet_types.sanitize_identifier), with an info issue
    recording the original name.

Multi-row headers, merged cells, and leading junk rows are deliberately
*not* handled: detecting them heuristically is guesswork that fails
silently and confusingly when it guesses wrong, and the honest fix is
30 seconds of cleanup in Excel before running the migration.

Type inference
--------------
Column types come from sampling real cells -- see
tgdatabridge.core.spreadsheet_types for the classification rules (and for why
text that looks numeric becomes numeric, why text with a *leading zero*
does not, and why only ISO-8601-shaped dates are recognized). Nullability
is observed the same way: a column with at least one empty cell in the
sample is nullable, and so is a column that was empty throughout (there
is no evidence it's NOT NULL, and guessing NOT NULL would break the
target insert on the first blank).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from tgdatabridge.core import spreadsheet_types as st
from tgdatabridge.core import type_mapping
from tgdatabridge.core.schema_model import Column, ConversionIssue, Schema, Table

if TYPE_CHECKING:
    from tgdatabridge.db.spreadsheet_connector import SpreadsheetConnector

DEFAULT_SAMPLE_ROWS = 10000


def introspect_schema(
    conn: "SpreadsheetConnector", schema_name: str, sample_rows: int = DEFAULT_SAMPLE_ROWS,
) -> Schema:
    """Build a Schema from every sheet in the connector's file.

    `schema_name` is what the tables' `schema` field is set to -- passed
    in by the caller (the GUI and CLI both pass `conn.schema_name`) for
    interface parity with every other introspect_schema() in this package.
    """
    schema = Schema(name=schema_name, source_engine="Excel/CSV")

    taken: set = set()
    multi_file = len(getattr(conn, "paths", []) or []) > 1
    prefix_all = _prefix_mode(conn, multi_file)
    for index, ref in enumerate(conn.sheet_refs()):
        table = _build_table(
            conn, schema_name, ref, index, sample_rows, taken, multi_file, prefix_all)
        if table is not None:
            schema.tables.append(table)

    return schema


def _prefix_mode(conn, multi_file: bool) -> bool:
    """Whether every table name should carry its workbook's name.

    `ConnectionParams.prefix_tables_with_file` decides when it is set;
    when it is None the answer is "yes if there is more than one input
    file". That default exists because the multi-workbook case is
    precisely where not prefixing hurts: several files exported by the
    same system have the same sheet names, so collision-only prefixing
    lets whichever file happened to be selected first keep the plain
    names while everyone else is prefixed -- asymmetric, and it changes
    if the selection order changes. A single file has nothing to be
    ambiguous with, so it keeps its long-standing plain names.
    """
    configured = getattr(getattr(conn, "params", None), "prefix_tables_with_file", None)
    if configured is None:
        return multi_file
    return bool(configured)


def _resolve_table_name(
    base: str, source_file_stem: str, taken: set, multi_file: bool, prefix_all: bool,
    issues: List[ConversionIssue], sheet_name: str,
) -> str:
    """Settle on the final, unique table name for one sheet.

    Sheet names are unique within a workbook but emphatically not across
    workbooks: several files exported by the same system all contain a
    "Sheet1" and a "Summary", and a set of monthly CSVs may be identical
    but for the directory they sit in. Two policies are available, and
    which one applies is decided by `_prefix_mode`.

    **prefix_all** -- every table is named `<file stem>_<sheet>`. Used by
    default whenever more than one file is read. Names are then symmetric
    (no file gets to be the special one that keeps the plain name) and
    independent of the order the files were selected in, and a reader of
    the target can see which workbook each table came from. The one
    exception is a sheet whose name already *is* the file's name -- every
    CSV, whose single implicit sheet is named after the file -- where
    prefixing would produce `north_north` and is skipped as redundant.

    **collision-only** -- the first sheet to claim a name keeps it, and a
    later one that wants the same name gets `<file stem>_<sheet>`. Used
    for a single file, and whenever explicitly configured. Prefixing a
    single-file job unconditionally would rename every table in output
    people already depend on, for consistency with nothing.

    Under both, a numeric suffix is appended if the chosen name is still
    taken (two files of the same name in different directories), and
    every rename is recorded as an issue -- matching how this module
    already reports a sanitized name. Nothing changes silently.
    """
    if prefix_all and base != source_file_stem:
        prefixed = st.sanitize_identifier(f"{source_file_stem}_{base}", fallback=base)
        candidate = _first_free(prefixed, taken)
        taken.add(candidate)
        if candidate != base:
            issues.append(ConversionIssue(
                "info",
                f"Sheet '{sheet_name}' was named '{candidate}' after its workbook, so tables "
                f"from different input files can't collide and it's clear which file each "
                f"came from.",
            ))
        return candidate

    if base not in taken:
        taken.add(base)
        return base

    prefixed = st.sanitize_identifier(f"{source_file_stem}_{base}", fallback=base)
    candidate = _first_free(prefixed, taken)
    taken.add(candidate)
    issues.append(ConversionIssue(
        "info",
        f"Another input file already produced a table named '{base}', so sheet "
        f"'{sheet_name}' was named '{candidate}' instead."
        if multi_file else
        f"A table named '{base}' already exists in this schema, so sheet "
        f"'{sheet_name}' was named '{candidate}' instead.",
    ))
    return candidate


def _first_free(preferred: str, taken: set) -> str:
    """`preferred`, or the first `preferred_2`, `preferred_3`... not yet
    claimed. Reached when two input files share a name but sit in
    different directories, so even the file-stem prefix isn't unique."""
    if preferred not in taken:
        return preferred
    suffix = 2
    while f"{preferred}_{suffix}" in taken:
        suffix += 1
    return f"{preferred}_{suffix}"


def _build_table(
    conn: "SpreadsheetConnector", schema_name: str, ref, sheet_index: int, sample_rows: int,
    taken: set, multi_file: bool, prefix_all: bool = False,
) -> Optional[Table]:
    """One sheet -> one Table, or None if the sheet holds nothing at all.

    `ref` is a SpreadsheetConnector.SheetRef -- the (file, sheet) pair,
    since a sheet name alone no longer identifies a sheet once several
    files can be read in one job.
    """
    sheet_name = ref.sheet_name
    # +1 so the header row itself doesn't eat one row's worth of sample.
    rows = list(conn.iter_raw_rows(sheet_name, limit=sample_rows + 1, file_path=ref.path))
    if not rows:
        return None

    table_name = st.sanitize_identifier(sheet_name, fallback=f"sheet_{sheet_index + 1}")
    issues: List[ConversionIssue] = []
    if table_name != sheet_name:
        issues.append(ConversionIssue(
            "info",
            f"Sheet '{sheet_name}' isn't a legal SQL identifier; the table was named "
            f"'{table_name}' instead.",
        ))
    table_name = _resolve_table_name(
        table_name, st.sanitize_identifier(ref.path.stem, fallback="file"),
        taken, multi_file, prefix_all, issues, sheet_name)

    header_row = rows[0]
    data_rows = [r for r in rows[1:] if not _is_blank_row(r)]

    column_names = _column_names(header_row, issues)
    if not column_names:
        issues.append(ConversionIssue(
            "warning",
            f"Sheet '{sheet_name}' has a completely empty first row, so no column names could be "
            "read; the sheet was skipped. Put a header row at the top and re-run.",
        ))
        return _empty_table(table_name, schema_name, sheet_name, issues, str(ref.path), multi_file)

    if not data_rows:
        issues.append(ConversionIssue(
            "warning",
            f"Sheet '{sheet_name}' has a header row but no data rows; its columns were all typed as "
            "text, since nothing could be observed to infer a real type from.",
        ))

    table = Table(
        name=table_name,
        schema=schema_name,
        source_collection=sheet_name,
        source_array_path=None,
        source_file=str(ref.path),
        row_count_estimate=len(data_rows),
        comment=_table_comment(sheet_name, ref.path.name, multi_file),
    )
    table.columns = _build_columns(column_names, data_rows, issues)
    table.issues = issues
    return table


def _table_comment(sheet_name: str, file_name: str, multi_file: bool) -> str:
    """Name the file too when there is more than one, so a reader of the
    generated DDL can tell which of fifty spreadsheets a table came from.
    Single-file wording is left exactly as it was."""
    if multi_file:
        return f"Imported from sheet '{sheet_name}' of '{file_name}'"
    return f"Imported from sheet '{sheet_name}'"


def _empty_table(
    table_name: str, schema_name: str, sheet_name: str, issues: List[ConversionIssue],
    source_file: Optional[str] = None, multi_file: bool = False,
) -> Table:
    table = Table(
        name=table_name, schema=schema_name, source_collection=sheet_name,
        source_file=source_file,
        comment=_table_comment(sheet_name, Path(source_file).name if source_file else "", multi_file),
    )
    table.issues = issues
    return table


def _column_names(header_row: tuple, issues: List[ConversionIssue]) -> List[str]:
    """Turn the header row into unique, legal column names.

    Trailing blank header cells are dropped entirely (a sheet whose used
    range is wider than its header is normal); a blank cell with real
    headers to the right of it is a genuine gap and becomes `column_N`.
    """
    trimmed = list(header_row)
    while trimmed and _is_blank_cell(trimmed[-1]):
        trimmed.pop()
    if not trimmed:
        return []

    names: List[str] = []
    seen: dict = {}
    for position, cell in enumerate(trimmed):
        if _is_blank_cell(cell):
            name = f"column_{position + 1}"
            issues.append(ConversionIssue(
                "warning",
                f"Header cell {position + 1} is blank; that column was named '{name}'.",
            ))
        else:
            raw = str(cell).strip()
            name = st.sanitize_identifier(raw, fallback=f"column_{position + 1}")
            if name != raw:
                issues.append(ConversionIssue(
                    "info",
                    f"Header '{raw}' isn't a legal SQL identifier; the column was named "
                    f"'{name}' instead.",
                ))

        # Case-insensitive duplicate detection: most target engines fold
        # or compare identifiers case-insensitively, so "Total" and
        # "total" would collide on the target even though Python sees two
        # distinct strings.
        key = name.lower()
        if key in seen:
            seen[key] += 1
            deduped = f"{name}_{seen[key]}"
            issues.append(ConversionIssue(
                "warning",
                f"More than one header resolves to the column name '{name}'; the duplicate was "
                f"renamed '{deduped}'.",
            ))
            name = deduped
            seen[name.lower()] = 1
        else:
            seen[key] = 1
        names.append(name)

    return names


def _build_columns(
    column_names: List[str], data_rows: List[tuple], issues: List[ConversionIssue],
) -> List[Column]:
    columns: List[Column] = []
    for position, name in enumerate(column_names):
        observed, max_len = _observe_column(data_rows, position)

        resolved = st.combine_types(observed)
        column_issues: List[ConversionIssue] = []
        if st.is_conflict(observed):
            kinds = ", ".join(sorted({t for t in observed if t != st.NULL}))
            column_issues.append(ConversionIssue(
                "warning",
                f"Column '{name}' mixes values of different kinds across the sampled rows "
                f"({kinds}) -- often a stray 'N/A' or note in an otherwise numeric or date column. "
                "It was typed as text, the only type that can hold all of them, so the migration "
                "won't fail part-way through. Clean the column up in the source file if you want a "
                "real numeric or date column on the target.",
            ))

        pivot_type, map_issues = type_mapping.from_spreadsheet(resolved, max_len)
        column_issues.extend(map_issues)

        # Every column is nullable, deliberately, even one with a value in
        # every single sampled row. Unlike a real source database there is
        # no declared NOT NULL constraint here to preserve -- it could only
        # ever be *guessed* from a sample, and that guess fails in the worst
        # possible way: Apply DDL succeeds, the migration runs for an hour,
        # and then row 40,000 turns out to have a blank cell and the whole
        # thing aborts. Same "fail safe, not loudly" principle
        # spreadsheet_types.combine_types uses for mixed-type columns. Add
        # NOT NULL on the target afterwards, where a violation is a cheap,
        # immediate error instead of a lost migration run.
        columns.append(Column(
            name=name, data_type=pivot_type, nullable=True, source_issues=column_issues,
        ))
    return columns


def _observe_column(data_rows: List[tuple], position: int) -> Tuple[List[str], Optional[int]]:
    """Classify every sampled cell in one column, returning
    `(type_names, max_string_length)`."""
    observed: List[str] = []
    max_len: Optional[int] = None

    for row in data_rows:
        cell = row[position] if position < len(row) else None
        kind, normalized = st.classify_value(cell)
        observed.append(kind)
        if kind == st.NULL:
            continue
        # Every type can end up rendered as text if the column turns out
        # to be mixed, so track the widest textual form of *any* value,
        # not just of the ones that classified as strings.
        text_length = len(normalized if isinstance(normalized, str) else str(normalized))
        max_len = text_length if max_len is None else max(max_len, text_length)

    return observed, max_len


def _is_blank_cell(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_blank_row(row: tuple) -> bool:
    return all(_is_blank_cell(value) for value in row)
