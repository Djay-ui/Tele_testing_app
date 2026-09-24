"""After "Apply DDL to Target" every table on the target is empty --
that step creates the schema, it does not copy rows.

Read without that context, "499 tables, all record counts zero" looks
exactly like a migration that silently did nothing, which is what it was
reported as. The tool now says it, in the pane and in the dialog.
"""
import pytest

from tgdatabridge.core.target_introspector import TargetObjects

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication            # noqa: E402

from tgdatabridge.gui.target_schema_tree import TargetSchemaTree  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tree(qt_app):
    return TargetSchemaTree()


def _heading(tree):
    return tree.topLevelItem(0).text(0)


def _rows(tree):
    top = tree.topLevelItem(0)
    return [top.child(i).text(0) for i in range(top.childCount())]


def test_an_empty_target_says_so_rather_than_looking_broken(tree):
    tree.load_objects(TargetObjects(
        tables=["accounts", "contacts"], row_counts={"accounts": 0, "contacts": 0}))
    assert "all empty" in _heading(tree)
    assert "no data migrated yet" in _heading(tree)
    assert all("empty" in row for row in _rows(tree))


def test_a_populated_target_shows_what_landed(tree):
    tree.load_objects(TargetObjects(
        tables=["accounts", "contacts"],
        row_counts={"accounts": 1500, "contacts": 0}))
    heading = _heading(tree)
    assert "1 with data" in heading
    assert "1,500" in heading
    rows = _rows(tree)
    assert "~1,500 rows" in rows[0]
    assert "empty" in rows[1]


def test_without_counts_the_pane_is_exactly_as_it_was(tree):
    """An engine whose introspector does not report counts must not start
    claiming every table is empty."""
    tree.load_objects(TargetObjects(tables=["accounts", "contacts"]))
    assert _heading(tree) == "Tables (2)"
    assert _rows(tree) == ["accounts", "contacts"]


def test_a_negative_estimate_is_not_shown_as_a_negative_row_count(tree):
    """PostgreSQL's reltuples is -1 for a table that has never been
    analysed -- "unknown", not "minus one rows"."""
    tree.load_objects(TargetObjects(tables=["fresh"], row_counts={"fresh": -1}))
    assert "-1" not in _rows(tree)[0]
    assert "empty" in _rows(tree)[0]


def test_the_other_categories_are_untouched(tree):
    tree.load_objects(TargetObjects(
        tables=["a"], views=["v"], sequences=["s"], routines=["r"],
        row_counts={"a": 0}))
    assert tree.topLevelItem(1).text(0) == "Views (1)"
    assert tree.topLevelItem(2).text(0) == "Sequences (1)"
    assert tree.topLevelItem(3).text(0) == "Routines / Triggers (1)"


def test_applying_ddl_tells_the_user_no_rows_have_moved():
    """The wording itself, so it cannot quietly disappear in a refactor."""
    import inspect

    from tgdatabridge.gui import main_window

    source = inspect.getsource(main_window.MainWindow._apply_ddl_text)
    assert "no rows have been copied yet" in source
    assert "4b. Migrate Data" in source
    assert "5. Apply Post-Load DDL" in source
