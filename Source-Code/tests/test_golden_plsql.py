"""Golden-file regression suite for PL/SQL -> {Postgres, SQL Server, Db2}
conversion.

Every unit test elsewhere in this test suite checks one specific rule in
isolation (a single builtin-function rewrite, a single manual marker, ...).
This file instead runs a handful of realistic, complete Oracle routines --
covering a basic procedure, a function with exceptions, a row trigger, a
package body, a nested subprogram, BULK COLLECT, DBMS_* calls, and CONNECT
BY -- end to end through convert_routine() for all three targets, and
compares the full output (converted DDL, status, and issue messages) against
a saved snapshot in tests/fixtures/plsql/expected/<engine>/<id>.sql.

The point isn't to assert today's output is "correct" in some abstract
sense (some of these fixtures deliberately land on MANUAL, e.g. the nested-
subprogram and BULK COLLECT-with-a-local-TYPE cases, and that's the right
outcome, not a bug) -- it's to make any *change* in output, for better or
worse, show up as an explicit, reviewable diff instead of silently shipping.
A rule change that regresses a construct that used to convert cleanly (or
that happens to break one of these specific shapes) fails loudly here even
if no other test happens to cover that exact combination.

To intentionally update a snapshot after a real, reviewed behavior change,
regenerate it: run the conversion for that fixture/engine and overwrite the
corresponding tests/fixtures/plsql/expected/<engine>/<id>.sql file, then
read the new diff to confirm every change is expected before committing it.
"""
import json
import os

from tgdatabridge.core.plsql_converter import convert_routine
from tgdatabridge.core.schema_model import Routine

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "plsql")
_EXPECTED_DIR = os.path.join(_FIXTURES_DIR, "expected")

_ENGINES = {
    "postgresql": "PostgreSQL",
    "sqlserver": "SQL Server",
    "db2": "DB2",
}


def _load_manifest():
    with open(os.path.join(_FIXTURES_DIR, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def _render_expected(result) -> str:
    """Renders a converted Routine into the same flat text format the saved
    snapshot files use: status, issue messages (order-independent -- sorted
    -- since issue-append order across independent rewrite passes is an
    implementation detail this suite shouldn't be sensitive to), then the
    converted DDL itself."""
    lines = [f"-- status: {result.status.value}", f"-- issues: {len(result.issues)}"]
    for issue in sorted(result.issues, key=lambda i: (i.severity, i.message)):
        lines.append(f"--   [{issue.severity}] {issue.message}")
    lines.append("")
    lines.append(result.converted_source)
    return "\n".join(lines) + "\n"


def _convert_fixture(entry: dict, source: str, target_engine: str):
    routine = Routine(
        name=entry["name"], schema=entry["schema"], kind=entry["kind"], source=source,
        table_name=entry.get("table_name"), timing=entry.get("timing"),
        events=entry.get("events", []), row_level=entry.get("row_level", True),
    )
    return convert_routine(routine, target_engine)


def _run_golden_case(fixture_id: str, engine_dir: str, target_engine: str):
    manifest = {entry["id"]: entry for entry in _load_manifest()}
    entry = manifest[fixture_id]
    with open(os.path.join(_FIXTURES_DIR, fixture_id + ".sql"), encoding="utf-8") as f:
        source = f.read()
    result = _convert_fixture(entry, source, target_engine)
    actual = _render_expected(result)

    expected_path = os.path.join(_EXPECTED_DIR, engine_dir, fixture_id + ".sql")
    # encoding="utf-8" is not optional here: several fixtures' issue messages
    # contain a real em dash (U+2014, e.g. "...instead -- add `USING
    # ERRCODE...`" in RAISE_APPLICATION_ERROR's warning). Without it, `open()`
    # falls back to locale.getpreferredencoding(), which is UTF-8 on Linux/Mac
    # (where this suite happened to always be run before) but NOT on Windows
    # (typically cp1252) -- the actual production platform this tool ships
    # on. A real Windows build (via BUILD-EXE.bat, which runs this suite
    # before packaging) misdecoded these three UTF-8 bytes into mojibake,
    # producing a spurious "golden mismatch" on three otherwise-correct
    # fixtures (function_with_exceptions/trigger_after_row/package_body) --
    # a test-harness bug, not a real behavior difference: the DDL and issue
    # text produced were byte-for-byte identical to the golden snapshot the
    # whole time, in both environments.
    with open(expected_path, encoding="utf-8") as f:
        # The saved snapshot's issue lines were written in the same
        # sorted order _render_expected produces, so a plain string
        # comparison is safe here without re-parsing/re-sorting the file.
        expected = f.read()

    assert actual == expected, (
        f"Golden mismatch for fixture '{fixture_id}' / {target_engine}. "
        f"If this is an intentional, reviewed behavior change, regenerate "
        f"{expected_path}.\n--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


def _make_test(fixture_id: str, engine_dir: str, target_engine: str):
    def _test():
        _run_golden_case(fixture_id, engine_dir, target_engine)
    _test.__name__ = f"test_golden_{fixture_id}_{engine_dir}"
    return _test


# Dynamically generate one test_golden_<fixture_id>_<engine> function per
# (fixture, engine) pair and register it at module level, so the test
# runner's "every top-level callable starting with test_" discovery picks
# each one up as its own independently reportable test (24 total: 8
# fixtures x 3 engines) rather than one big loop that stops at the first
# failure and hides the rest.
for _entry in _load_manifest():
    for _engine_dir, _target_engine in _ENGINES.items():
        _fn = _make_test(_entry["id"], _engine_dir, _target_engine)
        globals()[_fn.__name__] = _fn
del _entry, _engine_dir, _target_engine, _fn


def test_manifest_and_fixture_files_stay_in_sync():
    """Guards the golden suite's own bookkeeping: every manifest entry must
    have a matching .sql source file, and every .sql source file in the
    fixtures directory (other than under expected/) must be listed in the
    manifest -- otherwise a fixture could silently stop being exercised."""
    manifest_ids = {entry["id"] for entry in _load_manifest()}
    fixture_files = {
        fname[: -len(".sql")]
        for fname in os.listdir(_FIXTURES_DIR)
        if fname.endswith(".sql")
    }
    assert manifest_ids == fixture_files


def test_every_fixture_has_a_snapshot_for_every_engine():
    manifest_ids = [entry["id"] for entry in _load_manifest()]
    missing = [
        (fixture_id, engine_dir)
        for fixture_id in manifest_ids
        for engine_dir in _ENGINES
        if not os.path.isfile(os.path.join(_EXPECTED_DIR, engine_dir, fixture_id + ".sql"))
    ]
    assert missing == []
