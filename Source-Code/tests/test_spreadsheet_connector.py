"""Tests for tgdatabridge.db.spreadsheet_connector -- the Excel/CSV *source*
connector.

CSV/TSV coverage needs nothing but the standard library. The .xlsx tests
guard themselves with a plain `try: import openpyxl / except ImportError:
return` so the suite still passes on a machine without it installed
(same manual-guard pattern tests/test_cli_config.py uses for PyYAML --
this project's test runner is hand-rolled and has no pytest.importorskip).
"""
import datetime
import pathlib
import tempfile

from tgdatabridge.core.schema_model import Column, Table
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.spreadsheet_connector import SpreadsheetConnector


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp(prefix="tgdatabridge_sheet_"))


def _connector(path, schema=None):
    return SpreadsheetConnector(ConnectionParams(
        host="", port=0, database=str(path), username="", password="", schema=schema))


def _write_csv(text, name="data.csv"):
    path = _tmp_dir() / name
    path.write_text(text, encoding="utf-8")
    return path


def _openpyxl():
    """The openpyxl module, or None when it isn't installed."""
    try:
        import openpyxl
    except ImportError:
        return None
    return openpyxl


def _write_xlsx(sheets, name="book.xlsx"):
    """`sheets` is a list of (sheet_name, [row, ...]). Returns the path,
    or None when openpyxl isn't available."""
    openpyxl = _openpyxl()
    if openpyxl is None:
        return None
    path = _tmp_dir() / name
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sheets:
        worksheet = workbook.create_sheet(sheet_name)
        for row in rows:
            worksheet.append(list(row))
    workbook.save(path)
    return path


def _table(name, columns, sheet=None):
    table = Table(name=name, schema="s", source_collection=sheet or name)
    table.columns = [Column(name=n, data_type=t) for n, t in columns]
    return table


# ------------------------------------------------------------- connect

def test_connect_rejects_a_missing_file():
    conn = _connector(_tmp_dir() / "nope.csv")
    try:
        conn.connect()
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "nope.csv" in str(exc)


def test_connect_rejects_a_directory():
    directory = _tmp_dir()
    conn = _connector(directory)
    try:
        conn.connect()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Not a file" in str(exc)


def test_connect_rejects_legacy_xls_with_an_actionable_message():
    path = _tmp_dir() / "old.xls"
    path.write_bytes(b"whatever")
    conn = _connector(path)
    try:
        conn.connect()
        assert False, "expected ValueError"
    except ValueError as exc:
        message = str(exc)
        assert "legacy .xls" in message
        # The fix must be spelled out, not just the problem.
        assert "re-save it as .xlsx" in message


def test_connect_rejects_an_unsupported_extension():
    path = _tmp_dir() / "notes.docx"
    path.write_bytes(b"whatever")
    conn = _connector(path)
    try:
        conn.connect()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "unsupported extension" in str(exc)


def test_test_connection_reports_the_sheets_it_found():
    path = _write_csv("a,b\n1,2\n")
    ok, message = _connector(path).test_connection()
    assert ok is True
    assert "data.csv" in message


def test_test_connection_reports_failure_without_raising():
    ok, message = _connector(_tmp_dir() / "missing.csv").test_connection()
    assert ok is False
    assert "missing.csv" in message


# --------------------------------------------------------- schema_name

def test_schema_name_defaults_to_the_file_stem():
    conn = _connector(_write_csv("a\n1\n", name="sales_2024.csv"))
    conn.connect()
    assert conn.schema_name == "sales_2024"


def test_schema_name_uses_the_explicit_value_when_given():
    conn = _connector(_write_csv("a\n1\n"), schema="reporting")
    conn.connect()
    assert conn.schema_name == "reporting"


def test_schema_name_is_sanitized():
    conn = _connector(_write_csv("a\n1\n", name="Q1 sales (final).csv"))
    conn.connect()
    assert conn.schema_name == "Q1_sales_final"


# --------------------------------------------------------- sheet_names

def test_csv_has_exactly_one_sheet_named_after_the_file():
    conn = _connector(_write_csv("a,b\n1,2\n", name="people.csv"))
    conn.connect()
    assert conn.sheet_names() == ["people"]


def test_xlsx_reports_every_sheet_in_workbook_order():
    path = _write_xlsx([("First", [["a"], [1]]), ("Second", [["b"], [2]])])
    if path is None:
        return
    conn = _connector(path)
    conn.connect()
    assert conn.sheet_names() == ["First", "Second"]
    conn.close()


# -------------------------------------------------------- iter_raw_rows

def test_iter_raw_rows_includes_the_header():
    conn = _connector(_write_csv("id,name\n1,Alice\n2,Bob\n"))
    conn.connect()
    rows = list(conn.iter_raw_rows("data"))
    assert rows[0] == ("id", "name")
    assert rows[1] == ("1", "Alice")
    assert len(rows) == 3


def test_iter_raw_rows_honours_the_limit():
    conn = _connector(_write_csv("id\n1\n2\n3\n4\n"))
    conn.connect()
    assert len(list(conn.iter_raw_rows("data", limit=2))) == 2


def test_tsv_is_split_on_tabs():
    path = _write_csv("id\tname\n1\tAlice\n", name="data.tsv")
    conn = _connector(path)
    conn.connect()
    assert list(conn.iter_raw_rows("data"))[1] == ("1", "Alice")


def test_utf8_bom_is_stripped_from_the_first_header_cell():
    # Excel writes a BOM when it saves a CSV; left in place it would glue
    # itself to the first header and produce a column named "﻿id".
    path = _tmp_dir() / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfid,name\n1,Alice\n")
    conn = _connector(path)
    conn.connect()
    assert list(conn.iter_raw_rows("bom"))[0] == ("id", "name")


def test_iter_raw_rows_rejects_an_unknown_sheet_name():
    path = _write_xlsx([("Only", [["a"], [1]])])
    if path is None:
        return
    conn = _connector(path)
    conn.connect()
    try:
        list(conn.iter_raw_rows("Missing"))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "has no sheet named 'Missing'" in str(exc)
    conn.close()


# --------------------------------------------------- fetch_batches_table

def test_fetch_batches_table_skips_the_header_row():
    conn = _connector(_write_csv("id,name\n1,Alice\n2,Bob\n"))
    conn.connect()
    table = _table("data", [("id", "NUMBER(9)"), ("name", "VARCHAR2(20)")], sheet="data")
    batches = list(conn.fetch_batches_table(table))
    assert len(batches) == 1
    columns, rows = batches[0]
    assert columns == ["id", "name"]
    assert rows == [(1, "Alice"), (2, "Bob")]


def test_fetch_batches_table_respects_batch_size():
    conn = _connector(_write_csv("id\n1\n2\n3\n4\n5\n"))
    conn.connect()
    table = _table("data", [("id", "NUMBER(9)")], sheet="data")
    batches = list(conn.fetch_batches_table(table, batch_size=2))
    assert [len(rows) for _, rows in batches] == [2, 2, 1]


def test_fetch_batches_table_coerces_to_the_declared_column_type():
    # The column was typed as text (the sample had "N/A" in it), so the
    # numeric rows must arrive as text too.
    conn = _connector(_write_csv("amount\n42\nN/A\n"))
    conn.connect()
    table = _table("data", [("amount", "VARCHAR2(10)")], sheet="data")
    _, rows = list(conn.fetch_batches_table(table))[0]
    assert rows == [("42",), ("N/A",)]


def test_fetch_batches_table_skips_blank_rows():
    conn = _connector(_write_csv("id,name\n1,Alice\n,\n2,Bob\n"))
    conn.connect()
    table = _table("data", [("id", "NUMBER(9)"), ("name", "VARCHAR2(20)")], sheet="data")
    _, rows = list(conn.fetch_batches_table(table))[0]
    assert rows == [(1, "Alice"), (2, "Bob")]


def test_fetch_batches_table_pads_short_rows_and_truncates_long_ones():
    conn = _connector(_write_csv("a,b\n1\n2,3,4\n"))
    conn.connect()
    table = _table("data", [("a", "NUMBER(9)"), ("b", "NUMBER(9)")], sheet="data")
    _, rows = list(conn.fetch_batches_table(table))[0]
    assert rows == [(1, None), (2, 3)]


def test_fetch_batches_table_uses_source_collection_not_the_table_name():
    # The table is named after a *sanitized* sheet name, so migration has
    # to look the real sheet up through source_collection.
    path = _write_xlsx([("Order Items", [["id"], [1], [2]])])
    if path is None:
        return
    conn = _connector(path)
    conn.connect()
    table = _table("Order_Items", [("id", "NUMBER(9)")], sheet="Order Items")
    _, rows = list(conn.fetch_batches_table(table))[0]
    assert rows == [(1,), (2,)]
    conn.close()


def test_fetch_batches_table_reads_real_excel_cell_types():
    path = _write_xlsx([("S", [
        ["n", "when", "flag"],
        [7, datetime.datetime(2024, 3, 7), True],
    ])])
    if path is None:
        return
    conn = _connector(path)
    conn.connect()
    table = _table("S", [("n", "NUMBER(9)"), ("when", "TIMESTAMP"), ("flag", "BOOLEAN")], sheet="S")
    _, rows = list(conn.fetch_batches_table(table))[0]
    assert rows == [(7, datetime.datetime(2024, 3, 7), True)]
    conn.close()


def test_fetch_batches_table_on_a_header_only_sheet_yields_nothing():
    conn = _connector(_write_csv("id,name\n"))
    conn.connect()
    table = _table("data", [("id", "NUMBER(9)"), ("name", "VARCHAR2(20)")], sheet="data")
    assert list(conn.fetch_batches_table(table)) == []


# ---------------------------------------------------------- count_rows

def test_count_rows_excludes_header_and_blank_rows():
    conn = _connector(_write_csv("id\n1\n\n2\n3\n"))
    conn.connect()
    assert conn.count_rows("data") == 3


def test_count_rows_resolves_a_sanitized_table_name_back_to_its_sheet():
    path = _write_xlsx([("Order Items (2024)", [["id"], [1], [2], [3]])])
    if path is None:
        return
    conn = _connector(path)
    conn.connect()
    assert conn.count_rows("Order_Items_2024") == 3
    conn.close()


# -------------------------------------------------- unsupported by design

def test_execute_raises_because_there_is_no_sql_for_a_worksheet():
    conn = _connector(_write_csv("a\n1\n"))
    try:
        conn.execute("SELECT 1")
        assert False, "expected NotImplementedError"
    except NotImplementedError as exc:
        assert "fetch_batches_table" in str(exc)


def test_execute_ddl_raises_because_a_spreadsheet_cannot_be_a_target():
    conn = _connector(_write_csv("a\n1\n"))
    try:
        conn.execute_ddl("CREATE TABLE x (a INT)")
        assert False, "expected NotImplementedError"
    except NotImplementedError as exc:
        assert "source-only" in str(exc)


def test_close_is_safe_to_call_repeatedly():
    conn = _connector(_write_csv("a\n1\n"))
    conn.connect()
    conn.close()
    conn.close()
