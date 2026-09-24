"""Excel/CSV **source** connector -- reads local .xlsx/.xlsm/.csv/.tsv
files as if they were a source database, so spreadsheets can be migrated
into any target engine this tool supports.

This is the one connector in this package that is source-only and has no
server on the other end of it. That shapes almost everything below:

  - `ConnectionParams.database` carries a **file path**, not a database
    name, and `ConnectionParams.files` optionally carries a *list* of
    them (up to MAX_SOURCE_FILES); `host`, `port`, `username` and
    `password` are unused (there is nothing to authenticate against).
    `schema` optionally names the logical schema the generated tables
    land in -- see `schema_name` for what it defaults to.
  - `execute()` and `execute_ddl()` both raise NotImplementedError. There
    is no SQL dialect for a query against a worksheet to be written in
    (the same reason MongoConnector.execute() raises), and this can never
    be a *target*: writing a converted schema back into a spreadsheet is
    not a thing this tool does.
  - Data migration therefore uses the duck-typed `fetch_batches_table()`
    method migrator.migrate_table() already prefers when a source defines
    it -- introduced for MongoDB, reused here unchanged. A SQL string
    can't express "read sheet 'Order Items' out of this workbook" any
    more than it could express unwinding a Mongo array.

One sheet = one table; row 1 is the header; data starts at row 2. That
scope was chosen deliberately over trying to cope with merged cells,
multi-row headers and leading junk rows -- see
tgdatabridge.core.spreadsheet_introspector's docstring for the reasoning and for
what happens when a file doesn't match that shape.

Several files at once
---------------------
A migration whose source data is spread over many spreadsheets -- one
file per region, per month, per department -- used to mean running the
tool once per file, each run landing in its own schema. The connector now
accepts a list of files instead, and presents every sheet in every file
as one flat set of tables in a single schema, so one Convert + Apply DDL
+ Migrate Data pass covers the lot.

The cap is MAX_SOURCE_FILES (50). It exists because each Excel workbook
is held open for the life of the connection -- openpyxl's read_only mode
keeps memory per workbook modest but not zero -- and because a run
sourcing more files than that is almost always a directory picked by
mistake, which is better refused with a clear message than discovered
half an hour into a migration.

Two files may legitimately contain a sheet of the same name, so table
names are made unique at introspection time rather than here; see
spreadsheet_introspector._unique_table_name for that policy. This module
is responsible only for saying *which* (file, sheet) pairs exist and for
reading rows out of a specific one.

openpyxl is imported lazily inside `connect()`, the same way every other
connector in this package defers its driver import, so the GUI still
starts (and CSV files still work) on a machine without it installed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from tgdatabridge.core.schema_model import Table
from tgdatabridge.core.spreadsheet_types import coerce_for_pivot_type, sanitize_identifier
from tgdatabridge.db.base import ConnectionParams

EXCEL_SUFFIXES = (".xlsx", ".xlsm")
CSV_SUFFIXES = (".csv", ".tsv")
SUPPORTED_SUFFIXES = EXCEL_SUFFIXES + CSV_SUFFIXES

# How many rows the introspector reads per sheet to infer column types.
# Ten thousand is far more than the 1000 documents mongo_source_introspector
# samples, because reading rows off a local file is orders of magnitude
# cheaper than round-tripping to a database server -- and unlike Mongo's
# server-side $sample this is the *first* N rows, not a random selection,
# so a wide default meaningfully reduces the chance of missing a type that
# only appears further down the file.
DEFAULT_SAMPLE_ROWS = 10000

# Maximum number of input files one Excel/CSV source may be given. See
# this module's docstring for why there is a limit at all.
MAX_SOURCE_FILES = 50


@dataclass(frozen=True)
class SheetRef:
    """One readable sheet, identified by the file it lives in as well as
    its name -- a sheet name alone stopped being unique once several
    files could be read in one job."""

    path: Path
    sheet_name: str
    file_index: int


class SpreadsheetConnector:
    """Reads one or more local spreadsheet / delimited-text files as a
    source."""

    def __init__(self, params: ConnectionParams):
        self.params = params
        self.paths: List[Path] = _resolve_input_paths(params)
        # The first file. Retained as `self.path` because a single-file
        # job is still by far the common case and this keeps every
        # single-file code path (and its tests) reading exactly as before.
        self.path = self.paths[0] if self.paths else Path("")
        # Open workbooks, keyed by path -- only Excel files appear here.
        self._workbooks: Dict[Path, object] = {}
        self._is_excel: Dict[Path, bool] = {}
        # sanitized table name -> SheetRef, so count_rows() and
        # fetch_batches_table() (which receive a table *name*, or a Table)
        # can find their way back to the sheet it was derived from.
        self._sheet_by_table_name: Dict[str, SheetRef] = {}

    # --------------------------------------------------------- lifecycle

    def connect(self) -> None:
        if not self.paths:
            raise ValueError(
                "No input file was given. Pick at least one .xlsx/.xlsm/.csv/.tsv file.")
        if len(self.paths) > MAX_SOURCE_FILES:
            raise ValueError(
                f"{len(self.paths)} files were selected, but at most {MAX_SOURCE_FILES} can be "
                f"read in one migration. Split the job, or point it at fewer files."
            )

        for path in self.paths:
            self._validate(path)

        for path in self.paths:
            suffix = path.suffix.lower()
            is_excel = suffix in EXCEL_SUFFIXES
            self._is_excel[path] = is_excel
            if is_excel:
                openpyxl = _require_openpyxl(path)
                # read_only keeps memory flat on large workbooks; data_only
                # gives the last-calculated *value* of a formula cell rather
                # than the formula text, which is what should be migrated.
                self._workbooks[path] = openpyxl.load_workbook(
                    path, read_only=True, data_only=True)

        self._sheet_by_table_name = {
            sanitize_identifier(ref.sheet_name, fallback=f"sheet_{i + 1}"): ref
            for i, ref in enumerate(self.sheet_refs())
        }

    @staticmethod
    def _validate(path: Path) -> None:
        suffix = path.suffix.lower()
        if not path.exists():
            raise FileNotFoundError(f"No such file: {path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {path}")
        if suffix == ".xls":
            raise ValueError(
                f"'{path.name}' is a legacy .xls workbook, which this tool cannot read. "
                "Open it in Excel (or LibreOffice) and re-save it as .xlsx, then try again."
            )
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(
                f"'{path.name}' has an unsupported extension '{suffix}'. "
                f"Supported: {', '.join(SUPPORTED_SUFFIXES)}."
            )

    def close(self) -> None:
        for workbook in self._workbooks.values():
            try:
                workbook.close()
            except Exception:  # noqa: BLE001 -- closing must not mask a real error
                pass
        self._workbooks = {}

    def test_connection(self) -> Tuple[bool, str]:
        try:
            self.connect()
            refs = self.sheet_refs()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            self.close()

        if len(self.paths) == 1:
            names = [r.sheet_name for r in refs]
            return True, f"Read '{self.path.name}' ({len(names)} sheet(s): {', '.join(names)})"
        # With many files, listing every sheet would be unreadable -- the
        # per-file counts are what actually tell the user whether they
        # picked the right things.
        per_file = ", ".join(
            f"{p.name} ({sum(1 for r in refs if r.path == p)})" for p in self.paths[:5]
        )
        more = f", +{len(self.paths) - 5} more" if len(self.paths) > 5 else ""
        return True, (
            f"Read {len(self.paths)} files, {len(refs)} sheet(s) total: {per_file}{more}"
        )

    @property
    def schema_name(self) -> str:
        """The logical schema name the generated tables belong to.

        A spreadsheet has no schema concept of its own, so this is what
        the user typed if they typed anything. Failing that, a single
        file uses its own stem (`customers.xlsx` -> `customers`), which
        is the long-standing behaviour. Several files deliberately do
        *not*: naming a schema after whichever file happened to be first
        of fifty is arbitrary and actively misleading, so it falls back
        to the neutral "spreadsheet" instead.
        """
        explicit = (self.params.schema or "").strip()
        if explicit:
            return sanitize_identifier(explicit, fallback="spreadsheet")
        if len(self.paths) == 1:
            return sanitize_identifier(self.path.stem, fallback="spreadsheet")
        return "spreadsheet"

    # ------------------------------------------------------------ reading

    def sheet_refs(self) -> List[SheetRef]:
        """Every readable sheet across every input file, in file order and
        then workbook order. A CSV/TSV file has exactly one implicit
        "sheet" named after the file itself."""
        refs: List[SheetRef] = []
        for file_index, path in enumerate(self.paths):
            if self._is_excel.get(path):
                workbook = self._workbooks.get(path)
                if workbook is None:
                    continue
                for name in workbook.sheetnames:
                    refs.append(SheetRef(path=path, sheet_name=name, file_index=file_index))
            else:
                refs.append(SheetRef(path=path, sheet_name=path.stem, file_index=file_index))
        return refs

    def sheet_names(self) -> List[str]:
        """Sheet names across every input file, in order. Retained for
        callers that predate multi-file support; `sheet_refs()` is what
        anything needing to address a specific sheet should use, since
        these names are not unique once more than one file is involved."""
        return [ref.sheet_name for ref in self.sheet_refs()]

    def _resolve_sheet(self, sheet_name: str, file_path: Optional[Path] = None) -> SheetRef:
        refs = self.sheet_refs()
        if file_path is not None:
            resolved = Path(file_path)
            for ref in refs:
                if ref.path == resolved and ref.sheet_name == sheet_name:
                    return ref
            raise ValueError(
                f"'{resolved.name}' has no sheet named '{sheet_name}'."
            )
        matches = [ref for ref in refs if ref.sheet_name == sheet_name]
        if not matches:
            if len(self.paths) == 1:
                # Keep the single-file wording, which names the file and is
                # more useful than the multi-file phrasing below.
                known = ", ".join(r.sheet_name for r in refs)
                raise ValueError(
                    f"'{self.path.name}' has no sheet named '{sheet_name}' (sheets: {known}).")
            known = ", ".join(sorted({r.sheet_name for r in refs}))
            raise ValueError(f"No sheet named '{sheet_name}' in any input file (sheets: {known}).")
        # Ambiguity is possible only when the caller didn't say which file
        # it meant. Introspection always records the file on the Table, so
        # in practice this is reached only by a single-file caller.
        return matches[0]

    def iter_raw_rows(
        self, sheet_name: str, limit: Optional[int] = None,
        file_path: Optional[Path] = None,
    ) -> Iterator[tuple]:
        """Yield raw, uninterpreted rows (header row included) from one
        sheet -- the single reading primitive both introspection and
        migration are built on.

        `file_path` disambiguates when several input files contain a
        sheet of the same name. Omitting it keeps the original
        single-file signature working unchanged.
        """
        ref = self._resolve_sheet(sheet_name, file_path)
        if self._is_excel.get(ref.path):
            yield from self._iter_excel_rows(ref, limit)
        else:
            yield from self._iter_csv_rows(ref.path, limit)

    def _iter_excel_rows(self, ref: SheetRef, limit: Optional[int]) -> Iterator[tuple]:
        workbook = self._workbooks.get(ref.path)
        if workbook is None:
            raise RuntimeError("connect() must be called before reading rows.")
        if ref.sheet_name not in workbook.sheetnames:
            raise ValueError(
                f"'{ref.path.name}' has no sheet named '{ref.sheet_name}' "
                f"(sheets: {', '.join(workbook.sheetnames)})."
            )
        worksheet = workbook[ref.sheet_name]
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            if limit is not None and index >= limit:
                return
            yield tuple(row)

    def _iter_csv_rows(self, path: Path, limit: Optional[int]) -> Iterator[tuple]:
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        # utf-8-sig transparently eats the BOM Excel writes when it saves
        # a CSV, which would otherwise end up glued to the first header
        # cell and turn "id" into a column literally named "﻿id".
        with open(path, "r", encoding="utf-8-sig", newline="", errors="replace") as handle:
            for index, row in enumerate(csv.reader(handle, delimiter=delimiter)):
                if limit is not None and index >= limit:
                    return
                yield tuple(row)

    # ------------------------------------------------------- migration API

    def fetch_batches_table(
        self, table: Table, batch_size: int = 5000,
    ) -> Iterator[Tuple[List[str], List[tuple]]]:
        """Stream one table's rows as `(columns, rows)` batches, exactly
        like every other connector's `fetch_batches` -- migrator.
        migrate_table() hands each batch straight to `target.insert_batch()`
        unchanged. See this module's docstring for why this takes a Table
        rather than a SQL string.

        Every cell is coerced to match the column type inference settled
        on (see spreadsheet_types.coerce_for_pivot_type): inference ran
        against a *sample*, migration reads every row, and a row further
        down the file may well hold a value the sample never showed.

        Rows shorter than the header are padded with None and rows longer
        than it are truncated -- a trailing stray cell in one row of an
        otherwise-clean sheet shouldn't abort a migration.
        """
        columns = [c.name for c in table.columns]
        pivot_types = [c.data_type for c in table.columns]
        sheet_name = table.source_collection or table.name
        # source_file is None for a Table built before multi-file support
        # (or by a single-file job), in which case sheet-name lookup alone
        # is unambiguous -- there is only one file to find it in.
        file_path = Path(table.source_file) if table.source_file else None
        width = len(columns)

        batch: List[tuple] = []
        for index, raw_row in enumerate(self.iter_raw_rows(sheet_name, file_path=file_path)):
            if index == 0:
                continue  # header row -- already turned into `columns`
            if _is_blank_row(raw_row):
                continue
            padded = tuple(raw_row[:width]) + (None,) * max(0, width - len(raw_row))
            batch.append(tuple(
                coerce_for_pivot_type(value, pivot_types[i]) for i, value in enumerate(padded)
            ))
            if len(batch) >= batch_size:
                yield columns, batch
                batch = []
        if batch:
            yield columns, batch

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent row count for dry-run planning (see
        migrator.plan_schema). `schema` is accepted for signature parity
        with every other connector and unused -- a spreadsheet has no
        schema to qualify a sheet by.

        Counts data rows only: the header row and fully-blank rows are
        excluded, matching exactly what fetch_batches_table would yield.
        """
        ref = self._sheet_by_table_name.get(table)
        if ref is not None:
            rows = self.iter_raw_rows(ref.sheet_name, file_path=ref.path)
        else:
            rows = self.iter_raw_rows(table)
        count = 0
        for index, raw_row in enumerate(rows):
            if index == 0 or _is_blank_row(raw_row):
                continue
            count += 1
        return count

    # ------------------------------------------------- unsupported by design

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        raise NotImplementedError(
            "SpreadsheetConnector.execute() is not implemented -- there is no SQL dialect for a "
            "query against a worksheet to be written in. Introspection reads rows directly via "
            "iter_raw_rows() (see tgdatabridge.core.spreadsheet_introspector) and data migration uses "
            "fetch_batches_table() instead of a SQL-string fetch_batches()."
        )

    def execute_ddl(self, sql: str) -> None:
        raise NotImplementedError(
            "SpreadsheetConnector is a source-only connector -- a spreadsheet cannot be a migration "
            "*target*. Pick a real database engine as the target."
        )


def _resolve_input_paths(params: ConnectionParams) -> List[Path]:
    """Work out the ordered, de-duplicated list of files to read.

    `params.files` wins when set; otherwise the single `params.database`
    path is used, which is what every pre-multi-file caller passes.

    Duplicates are dropped rather than read twice: a user multi-selecting
    in the file dialog, or a config listing the same file under two
    spellings, would otherwise get two sets of tables from one file, the
    second lot silently renamed by the collision policy.
    """
    raw: List[str]
    if params.files:
        raw = [str(f) for f in params.files if str(f).strip()]
    else:
        raw = [params.database] if (params.database or "").strip() else []

    ordered: List[Path] = []
    seen = set()
    for item in raw:
        path = Path(item)
        try:
            key = path.resolve()
        except OSError:
            # An unresolvable path is still worth keeping so connect()
            # can report it properly rather than silently dropping it.
            key = path
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def _require_openpyxl(path: Path):
    try:
        import openpyxl  # lazy: CSV files work without it installed
    except ImportError as exc:
        raise ImportError(
            f"Reading .xlsx/.xlsm files needs the 'openpyxl' package, which isn't installed "
            f"(needed for '{path.name}'). Install it with: pip install openpyxl -- or export "
            "the sheet as .csv, which needs no extra package."
        ) from exc
    return openpyxl


def _is_blank_row(row: tuple) -> bool:
    """True for a row where every cell is empty. Trailing blank rows are
    extremely common in hand-maintained spreadsheets (and openpyxl reports
    them right up to the sheet's last *formatted* cell, which can be
    thousands of rows past the last real one), so they're skipped rather
    than migrated as rows of NULLs."""
    return all(value is None or (isinstance(value, str) and not value.strip()) for value in row)
