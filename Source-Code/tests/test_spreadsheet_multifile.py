"""Multi-file Excel/CSV source: several input files in one migration.

The source engine used to take exactly one file, so a job whose data was
spread over many spreadsheets -- one per region, per month, per
department -- meant one run per file, each landing in its own schema.
The connector now accepts a list (up to
SpreadsheetConnector.MAX_SOURCE_FILES) and presents every sheet in every
file as one flat set of tables in a single schema.

CSV files carry the whole suite here: they need no third-party package,
so these run anywhere, and every multi-file concern -- ordering,
de-duplication, name collisions, per-file row reads, the cap -- is
independent of whether a given file is a workbook or delimited text.
Excel-specific reading is already covered by test_spreadsheet_connector.
"""
import pytest

from tgdatabridge.core import spreadsheet_introspector
from tgdatabridge.db.base import ConnectionParams
from tgdatabridge.db.spreadsheet_connector import MAX_SOURCE_FILES, SpreadsheetConnector


def _csv(tmp_path, name, header="id,name", rows=("1,Alice", "2,Bob")):
    path = tmp_path / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _connector(*paths, schema=None):
    params = ConnectionParams(
        host="", port=0, database=str(paths[0]) if paths else "",
        username="", password="", schema=schema,
        files=[str(p) for p in paths] or None,
    )
    conn = SpreadsheetConnector(params)
    conn.connect()
    return conn


# ------------------------------------------------------ backward compatible

def test_single_file_via_database_field_is_unchanged(tmp_path):
    """The pre-existing calling convention -- one path in `database`, no
    `files` at all -- must behave exactly as it did."""
    path = _csv(tmp_path, "customers.csv")
    params = ConnectionParams(
        host="", port=0, database=str(path), username="", password="", schema=None)
    conn = SpreadsheetConnector(params)
    conn.connect()

    assert conn.paths == [path]
    assert conn.path == path
    assert conn.sheet_names() == ["customers"]
    assert conn.schema_name == "customers"          # file stem, as before
    ok, message = conn.test_connection()
    assert ok and "customers.csv" in message


def test_single_file_via_files_list_matches_the_database_field(tmp_path):
    path = _csv(tmp_path, "customers.csv")
    conn = _connector(path)
    assert conn.schema_name == "customers"
    assert conn.sheet_names() == ["customers"]


# ------------------------------------------------------------- many files

def test_every_sheet_across_files_becomes_a_table(tmp_path):
    a = _csv(tmp_path, "north.csv")
    b = _csv(tmp_path, "south.csv")
    c = _csv(tmp_path, "east.csv")
    conn = _connector(a, b, c)

    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    assert [t.name for t in schema.tables] == ["north", "south", "east"]
    # File order is preserved, not sorted -- the user chose an order.
    assert [t.source_file for t in schema.tables] == [str(a), str(b), str(c)]


def test_multi_file_schema_name_does_not_borrow_the_first_file(tmp_path):
    """Naming a schema after whichever of fifty files happened to be first
    is arbitrary and misleading, so it falls back to a neutral name."""
    conn = _connector(_csv(tmp_path, "north.csv"), _csv(tmp_path, "south.csv"))
    assert conn.schema_name == "spreadsheet"


def test_explicit_schema_still_wins_for_many_files(tmp_path):
    conn = _connector(
        _csv(tmp_path, "north.csv"), _csv(tmp_path, "south.csv"), schema="sales_2026")
    assert conn.schema_name == "sales_2026"


def test_rows_are_read_from_the_right_file(tmp_path):
    """The core risk of multi-file support: a sheet-name lookup that
    silently reads the wrong file's rows."""
    a = _csv(tmp_path, "north.csv", rows=("1,Alice", "2,Bob"))
    b = _csv(tmp_path, "south.csv", rows=("3,Carol",))
    conn = _connector(a, b)

    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    by_name = {t.name: t for t in schema.tables}

    north_rows = [r for _cols, batch in conn.fetch_batches_table(by_name["north"]) for r in batch]
    south_rows = [r for _cols, batch in conn.fetch_batches_table(by_name["south"]) for r in batch]
    assert [r[1] for r in north_rows] == ["Alice", "Bob"]
    assert [r[1] for r in south_rows] == ["Carol"]
    assert conn.count_rows("north") == 2
    assert conn.count_rows("south") == 1


def test_duplicate_paths_are_read_once(tmp_path):
    """A user multi-selecting the same file twice, or a config listing it
    under two spellings, would otherwise produce two sets of tables from
    one file -- the second lot renamed by the collision policy."""
    path = _csv(tmp_path, "customers.csv")
    conn = _connector(path, path, tmp_path / "." / "customers.csv")
    assert conn.paths == [path]
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    assert [t.name for t in schema.tables] == ["customers"]


# ------------------------------------------------------- name collisions

def test_colliding_sheet_names_are_disambiguated_by_file(tmp_path):
    """Two files may legitimately contain a sheet of the same name -- two
    CSVs called data.csv in different folders, or two workbooks each with
    a Sheet1."""
    d1 = tmp_path / "north"
    d2 = tmp_path / "south"
    d1.mkdir()
    d2.mkdir()
    a = _csv(d1, "data.csv", rows=("1,Alice",))
    b = _csv(d2, "data.csv", rows=("2,Bob",))
    conn = _connector(a, b)

    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    names = [t.name for t in schema.tables]
    assert len(names) == len(set(names)), f"table names collided: {names}"
    assert names[0] == "data"          # first claimant keeps the plain name
    assert names[1] != "data"

    # The rename is recorded rather than applied silently.
    messages = [i.message for i in schema.tables[1].issues]
    assert any("already produced a table named 'data'" in m for m in messages)

    # ...and each still reads its own file's rows.
    rows_a = [r for _c, b_ in conn.fetch_batches_table(schema.tables[0]) for r in b_]
    rows_b = [r for _c, b_ in conn.fetch_batches_table(schema.tables[1]) for r in b_]
    assert rows_a[0][1] == "Alice"
    assert rows_b[0][1] == "Bob"


def test_three_way_collision_still_produces_unique_names(tmp_path):
    dirs = []
    for i in range(3):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(_csv(d, "data.csv", rows=(f"{i},Name{i}",)))
    conn = _connector(*dirs)
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    names = [t.name for t in schema.tables]
    assert len(names) == len(set(names)) == 3


def test_single_file_table_names_are_not_prefixed(tmp_path):
    """Prefixing unconditionally would rename every table in every
    existing single-file job -- a silent breaking change to output people
    already depend on."""
    conn = _connector(_csv(tmp_path, "customers.csv"))
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    assert [t.name for t in schema.tables] == ["customers"]
    assert schema.tables[0].issues == []


# ---------------------------------------------------------------- the cap

def test_fifty_files_are_accepted(tmp_path):
    paths = [_csv(tmp_path, f"file_{i:02d}.csv", rows=(f"{i},Name{i}",))
             for i in range(MAX_SOURCE_FILES)]
    conn = _connector(*paths)
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)

    assert MAX_SOURCE_FILES == 50
    assert len(schema.tables) == 50
    assert len({t.name for t in schema.tables}) == 50
    # Spot-check that the last file is genuinely readable, not just listed.
    last = schema.tables[-1]
    rows = [r for _c, b in conn.fetch_batches_table(last) for r in b]
    assert rows[0][1] == "Name49"


def test_fifty_one_files_are_refused_with_a_clear_message(tmp_path):
    paths = [_csv(tmp_path, f"file_{i:02d}.csv") for i in range(MAX_SOURCE_FILES + 1)]
    params = ConnectionParams(
        host="", port=0, database=str(paths[0]), username="", password="",
        files=[str(p) for p in paths])
    with pytest.raises(ValueError) as exc:
        SpreadsheetConnector(params).connect()
    assert "51" in str(exc.value) and str(MAX_SOURCE_FILES) in str(exc.value)


def test_no_files_at_all_is_refused(tmp_path):
    params = ConnectionParams(host="", port=0, database="", username="", password="")
    with pytest.raises(ValueError) as exc:
        SpreadsheetConnector(params).connect()
    assert "at least one" in str(exc.value)


def test_one_bad_file_among_good_ones_is_reported_by_name(tmp_path):
    """A 50-file job must not fail with a message that leaves the user
    hunting for which file was wrong."""
    good = _csv(tmp_path, "good.csv")
    bad = tmp_path / "notes.docx"
    bad.write_text("nope", encoding="utf-8")
    params = ConnectionParams(
        host="", port=0, database=str(good), username="", password="",
        files=[str(good), str(bad)])
    with pytest.raises(ValueError) as exc:
        SpreadsheetConnector(params).connect()
    assert "notes.docx" in str(exc.value)


def test_missing_file_among_good_ones_is_reported_by_name(tmp_path):
    good = _csv(tmp_path, "good.csv")
    missing = tmp_path / "gone.csv"
    params = ConnectionParams(
        host="", port=0, database=str(good), username="", password="",
        files=[str(good), str(missing)])
    with pytest.raises(FileNotFoundError) as exc:
        SpreadsheetConnector(params).connect()
    assert "gone.csv" in str(exc.value)


# ------------------------------------------------------------- reporting

def test_test_connection_summarises_many_files(tmp_path):
    paths = [_csv(tmp_path, f"f{i}.csv") for i in range(7)]
    conn = SpreadsheetConnector(ConnectionParams(
        host="", port=0, database=str(paths[0]), username="", password="",
        files=[str(p) for p in paths]))
    ok, message = conn.test_connection()
    assert ok
    assert "7 files" in message
    assert "+2 more" in message      # first five named, remainder counted


def test_table_comment_names_the_file_only_when_there_are_several(tmp_path):
    single = _connector(_csv(tmp_path, "customers.csv"))
    schema = spreadsheet_introspector.introspect_schema(single, single.schema_name)
    assert schema.tables[0].comment == "Imported from sheet 'customers'"

    multi = _connector(_csv(tmp_path, "north.csv"), _csv(tmp_path, "south.csv"))
    schema = spreadsheet_introspector.introspect_schema(multi, multi.schema_name)
    assert schema.tables[0].comment == "Imported from sheet 'north' of 'north.csv'"


# ------------------------------------------------------------ CLI config

def test_cli_config_accepts_a_files_list(tmp_path):
    from tgdatabridge.cli.config import _parse_connection
    conn = _parse_connection(
        {"engine": "Excel/CSV", "files": ["a.xlsx", "b.csv"]}, "source",
        valid_engines=("Excel/CSV",))
    assert conn.files == ["a.xlsx", "b.csv"]
    # `database` keeps carrying the first file, so the checkpoint key and
    # every log line that reads it keep working unchanged.
    assert conn.database == "a.xlsx"


def test_cli_config_still_accepts_a_single_database_path(tmp_path):
    from tgdatabridge.cli.config import _parse_connection
    conn = _parse_connection(
        {"engine": "Excel/CSV", "database": "only.xlsx"}, "source",
        valid_engines=("Excel/CSV",))
    assert conn.database == "only.xlsx"
    assert conn.files == []


def test_cli_config_rejects_both_database_and_files():
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Excel/CSV", "database": "a.xlsx", "files": ["b.xlsx"]}, "source",
            valid_engines=("Excel/CSV",))
    assert "one or the other" in str(exc.value)


def test_cli_config_rejects_a_files_list_over_the_cap():
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Excel/CSV", "files": [f"f{i}.csv" for i in range(MAX_SOURCE_FILES + 1)]},
            "source", valid_engines=("Excel/CSV",))
    assert str(MAX_SOURCE_FILES) in str(exc.value)


def test_cli_config_rejects_a_files_string_rather_than_a_list():
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Excel/CSV", "files": "a.xlsx"}, "source",
            valid_engines=("Excel/CSV",))
    assert "must be a list" in str(exc.value)


def test_cli_connection_params_carry_the_file_list():
    from tgdatabridge.cli.config import CliConnectionConfig, resolve_connection_params
    params = resolve_connection_params(
        CliConnectionConfig(engine="Excel/CSV", database="a.xlsx", files=["a.xlsx", "b.csv"]))
    assert params.files == ["a.xlsx", "b.csv"]
    assert params.database == "a.xlsx"


# ------------------------------------------------- real workbooks (openpyxl)

def _xlsx(tmp_path, name, sheets):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(list(row))
    path = tmp_path / name
    wb.save(path)
    return path


def test_two_workbooks_each_with_sheet1(tmp_path):
    """The realistic collision: two systems both export a workbook whose
    first sheet is called Sheet1."""
    a = _xlsx(tmp_path, "north.xlsx", {"Sheet1": [("id", "name"), (1, "Alice")]})
    b = _xlsx(tmp_path, "south.xlsx", {"Sheet1": [("id", "name"), (2, "Bob")]})
    conn = _connector(a, b)

    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    names = [t.name for t in schema.tables]
    assert len(names) == len(set(names)) == 2
    rows_a = [r for _c, batch in conn.fetch_batches_table(schema.tables[0]) for r in batch]
    rows_b = [r for _c, batch in conn.fetch_batches_table(schema.tables[1]) for r in batch]
    assert rows_a[0][1] == "Alice"
    assert rows_b[0][1] == "Bob"
    conn.close()


def test_mixed_workbooks_and_csv_in_one_job(tmp_path):
    xlsx = _xlsx(tmp_path, "book.xlsx", {
        "Orders": [("id", "total"), (1, 9.5)],
        "Items": [("id", "sku"), (1, "A-1")],
    })
    csv_path = _csv(tmp_path, "refunds.csv", header="id,amount", rows=("1,3.25",))
    conn = _connector(xlsx, csv_path)

    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    # Several files, so every workbook sheet carries its file's name. The
    # CSV does not: its single implicit sheet is already named after the
    # file, and "refunds_refunds" would be nonsense.
    assert [t.name for t in schema.tables] == ["book_Orders", "book_Items", "refunds"]
    refunds = schema.tables[-1]
    rows = [r for _c, batch in conn.fetch_batches_table(refunds) for r in batch]
    assert len(rows) == 1
    conn.close()


def test_cli_config_rejects_files_for_a_network_engine():
    """`files` is meaningless for Oracle/Postgres/etc -- silently ignoring
    it would let a typo'd config run against the wrong thing."""
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Oracle", "host": "h", "port": 1521, "database": "d",
             "username": "u", "password_env": "P", "files": ["a.xlsx"]},
            "source", valid_engines=("Oracle", "Excel/CSV"))
    assert "only applies to a file-based source engine" in str(exc.value)


# ------------------------------------------------- naming mode (prefixing)

def _xlsx_book(tmp_path, name, sheets):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(list(row))
    path = tmp_path / name
    wb.save(path)
    return path


def _connector_mode(*paths, prefix=None, schema=None):
    params = ConnectionParams(
        host="", port=0, database=str(paths[0]), username="", password="", schema=schema,
        files=[str(p) for p in paths], prefix_tables_with_file=prefix)
    conn = SpreadsheetConnector(params)
    conn.connect()
    return conn


def _names(conn):
    return [t.name for t in
            spreadsheet_introspector.introspect_schema(conn, conn.schema_name).tables]


def test_many_workbooks_prefix_every_table_by_default(tmp_path):
    """The case this option exists for: the same report exported per
    region, so every workbook has identical sheet names. Names must be
    symmetric -- no file gets to be the one that keeps the plain name."""
    north = _xlsx_book(tmp_path, "sales_north.xlsx", {
        "Sheet1": [("id",), (1,)], "Customers": [("id",), (1,)]})
    south = _xlsx_book(tmp_path, "sales_south.xlsx", {
        "Sheet1": [("id",), (2,)], "Customers": [("id",), (2,)]})
    conn = _connector_mode(north, south)
    assert _names(conn) == [
        "sales_north_Sheet1", "sales_north_Customers",
        "sales_south_Sheet1", "sales_south_Customers",
    ]
    conn.close()


def test_prefixed_names_do_not_depend_on_selection_order(tmp_path):
    """Reordering the file selection must not change which workbook wins
    a plain name -- there are no plain names to win."""
    north = _xlsx_book(tmp_path, "sales_north.xlsx", {"Customers": [("id",), (1,)]})
    south = _xlsx_book(tmp_path, "sales_south.xlsx", {"Customers": [("id",), (2,)]})

    forwards = _connector_mode(north, south)
    backwards = _connector_mode(south, north)
    assert sorted(_names(forwards)) == sorted(_names(backwards))
    assert set(_names(forwards)) == {"sales_north_Customers", "sales_south_Customers"}
    forwards.close()
    backwards.close()


def test_a_single_workbook_is_never_prefixed_by_default(tmp_path):
    """Nothing about a one-file job changes -- prefixing there would
    rename tables in output people already depend on."""
    book = _xlsx_book(tmp_path, "sales.xlsx", {
        "Customers": [("id",), (1,)], "Orders": [("id",), (1,)]})
    conn = _connector_mode(book)
    assert _names(conn) == ["Customers", "Orders"]
    conn.close()


def test_prefixing_can_be_turned_off_for_many_files(tmp_path):
    """Opting out restores the original collision-only behaviour: the
    first claimant keeps the plain name."""
    north = _xlsx_book(tmp_path, "sales_north.xlsx", {"Customers": [("id",), (1,)]})
    south = _xlsx_book(tmp_path, "sales_south.xlsx", {"Customers": [("id",), (2,)]})
    conn = _connector_mode(north, south, prefix=False)
    assert _names(conn) == ["Customers", "sales_south_Customers"]
    conn.close()


def test_prefixing_can_be_forced_for_a_single_file(tmp_path):
    book = _xlsx_book(tmp_path, "sales.xlsx", {"Customers": [("id",), (1,)]})
    conn = _connector_mode(book, prefix=True)
    assert _names(conn) == ["sales_Customers"]
    conn.close()


def test_a_csv_is_not_prefixed_with_its_own_name(tmp_path):
    """A CSV's single implicit sheet is already named after the file, so
    prefixing would produce 'north_north'."""
    conn = _connector_mode(_csv(tmp_path, "north.csv"), _csv(tmp_path, "south.csv"))
    assert _names(conn) == ["north", "south"]


def test_two_identically_named_files_still_get_unique_names(tmp_path):
    """Prefixing by file stem doesn't help when the stems are the same --
    a numeric suffix is the last resort."""
    d1 = tmp_path / "q1"
    d2 = tmp_path / "q2"
    d1.mkdir()
    d2.mkdir()
    a = _xlsx_book(d1, "sales.xlsx", {"Customers": [("id",), (1,)]})
    b = _xlsx_book(d2, "sales.xlsx", {"Customers": [("id",), (2,)]})
    conn = _connector_mode(a, b)
    names = _names(conn)
    assert names == ["sales_Customers", "sales_Customers_2"]
    conn.close()


def test_prefixed_tables_still_read_their_own_workbook(tmp_path):
    """Renaming must not disturb which sheet a table's rows come from."""
    north = _xlsx_book(tmp_path, "sales_north.xlsx", {"Customers": [("id", "name"), (1, "North")]})
    south = _xlsx_book(tmp_path, "sales_south.xlsx", {"Customers": [("id", "name"), (2, "South")]})
    conn = _connector_mode(north, south)
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    for table, expected in zip(schema.tables, ["North", "South"]):
        rows = [r for _c, batch in conn.fetch_batches_table(table) for r in batch]
        assert rows[0][1] == expected
    conn.close()


def test_the_rename_is_recorded_as_an_issue(tmp_path):
    north = _xlsx_book(tmp_path, "sales_north.xlsx", {"Customers": [("id",), (1,)]})
    south = _xlsx_book(tmp_path, "sales_south.xlsx", {"Customers": [("id",), (2,)]})
    conn = _connector_mode(north, south)
    schema = spreadsheet_introspector.introspect_schema(conn, conn.schema_name)
    messages = [i.message for t in schema.tables for i in t.issues]
    assert any("after its workbook" in m for m in messages)
    assert any("sales_south_Customers" in m for m in messages)
    conn.close()


def test_cli_config_accepts_prefix_tables_with_file():
    from tgdatabridge.cli.config import _parse_connection, resolve_connection_params
    conn = _parse_connection(
        {"engine": "Excel/CSV", "files": ["a.xlsx", "b.xlsx"],
         "prefix_tables_with_file": False},
        "source", valid_engines=("Excel/CSV",))
    assert conn.prefix_tables_with_file is False
    assert resolve_connection_params(conn).prefix_tables_with_file is False


def test_cli_config_defaults_prefix_to_none():
    """None means 'decide from the file count', which is what makes a
    single-file job keep its plain names without the config saying so."""
    from tgdatabridge.cli.config import _parse_connection
    conn = _parse_connection(
        {"engine": "Excel/CSV", "files": ["a.xlsx"]}, "source", valid_engines=("Excel/CSV",))
    assert conn.prefix_tables_with_file is None


def test_cli_config_rejects_a_non_boolean_prefix():
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Excel/CSV", "files": ["a.xlsx"], "prefix_tables_with_file": "yes"},
            "source", valid_engines=("Excel/CSV",))
    assert "true or false" in str(exc.value)


def test_cli_config_rejects_prefix_for_a_network_engine():
    from tgdatabridge.cli.config import CliConfigError, _parse_connection
    with pytest.raises(CliConfigError) as exc:
        _parse_connection(
            {"engine": "Oracle", "host": "h", "port": 1521, "database": "d",
             "username": "u", "password_env": "P", "prefix_tables_with_file": True},
            "source", valid_engines=("Oracle", "Excel/CSV"))
    assert "only applies to a file-based source engine" in str(exc.value)
