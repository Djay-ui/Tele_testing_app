"""Unticking an object in the source tree must actually leave it out.

The checkboxes existed but only the data-migration step read them: the
generated DDL was always for the whole loaded schema, and the tick state
was reset every time the tree was rebuilt after a conversion.
"""
import pytest

from tgdatabridge.core.ddl_generator import generate_schema_ddl
from tgdatabridge.core.schema_model import (
    Column, Constraint, Routine, Schema, Sequence, Table, View,
)
from tgdatabridge.core.schema_subset import subset_schema, sync_conversion_results


def _schema():
    schema = Schema(name="dbo", source_engine="SQL Server", target_engine="MySQL")
    employees = Table(name="Employees", schema="dbo", columns=[
        Column(name="EmployeeID", data_type="NUMBER(10)", nullable=False, identity=True),
        Column(name="FirstName", data_type="VARCHAR2(50)"),
    ], constraints=[
        Constraint(name="PK_Employees", kind="PRIMARY KEY", columns=["EmployeeID"]),
    ])
    attendance = Table(name="EmployeeAttendance", schema="dbo", columns=[
        Column(name="AttendanceID", data_type="NUMBER(10)", nullable=False, identity=True),
        Column(name="EmployeeID", data_type="NUMBER(10)", nullable=False),
    ], constraints=[
        Constraint(name="PK_Attendance", kind="PRIMARY KEY", columns=["AttendanceID"]),
        Constraint(name="FK_Attendance_Employees", kind="FOREIGN KEY",
                   columns=["EmployeeID"], ref_table="Employees",
                   ref_columns=["EmployeeID"]),
    ])
    projects = Table(name="Projects", schema="dbo", columns=[
        Column(name="ProjectID", data_type="NUMBER(10)", nullable=False),
    ])
    schema.tables = [employees, attendance, projects]
    schema.views = [
        View(name="vw_EmployeeDetails", schema="dbo", source_engine="SQL Server",
             definition="SELECT EmployeeID FROM Employees"),
        View(name="vw_ProjectSummary", schema="dbo", source_engine="SQL Server",
             definition="SELECT ProjectID FROM Projects"),
    ]
    schema.sequences = [Sequence(name="seq_a", schema="dbo")]
    schema.routines = [
        Routine(name="usp_GetEmployees", schema="dbo", kind="PROCEDURE",
                source="...", source_engine="SQL Server"),
        Routine(name="trg_EmployeeSalaryUpdate", schema="dbo", kind="TRIGGER",
                source="...", source_engine="SQL Server", table_name="Employees"),
    ]
    return schema


def test_no_selection_arguments_keeps_everything():
    schema = _schema()
    subset, notes = subset_schema(schema)
    assert [t.name for t in subset.tables] == [t.name for t in schema.tables]
    assert len(subset.views) == 2 and len(subset.routines) == 2
    assert notes == []


def test_an_unticked_table_is_not_in_the_generated_ddl():
    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Projects"]
    subset, _notes = subset_schema(schema, tables=keep)
    ddl, _issues = generate_schema_ddl(subset, "MySQL")
    assert "`Employees`" in ddl
    assert "`Projects`" not in ddl


def test_a_foreign_key_into_an_unticked_table_is_dropped_with_a_note():
    """Otherwise the target answers 1005/150 or 3734 -- a failure caused
    by the tool, not by the user's selection."""
    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Employees"]
    subset, notes = subset_schema(schema, tables=keep)
    child = next(t for t in subset.tables if t.name == "EmployeeAttendance")
    assert [c.name for c in child.constraints] == ["PK_Attendance"]
    assert any("FK_Attendance_Employees" in n and "Employees" in n for n in notes)
    ddl, _issues = generate_schema_ddl(subset, "MySQL")
    assert "FK_Attendance_Employees" not in ddl


def test_the_loaded_schema_is_never_mutated():
    """Untick, convert, re-tick, convert again -- the second run must see
    the original object graph."""
    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Employees"]
    subset_schema(schema, tables=keep)
    child = next(t for t in schema.tables if t.name == "EmployeeAttendance")
    assert [c.name for c in child.constraints] == [
        "PK_Attendance", "FK_Attendance_Employees"]
    again, notes = subset_schema(schema)
    child_again = next(t for t in again.tables if t.name == "EmployeeAttendance")
    assert len(child_again.constraints) == 2
    assert notes == []


def test_a_trigger_whose_table_is_unticked_is_dropped_with_a_note():
    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Employees"]
    subset, notes = subset_schema(schema, tables=keep)
    assert [r.name for r in subset.routines] == ["usp_GetEmployees"]
    assert any("trg_EmployeeSalaryUpdate" in n for n in notes)


def test_a_view_over_an_unticked_table_is_left_out_with_a_note():
    """A view's body is resolved when it is created, so keeping it would
    guarantee "Table 'Projects' doesn't exist" mid-script."""
    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Projects"]
    subset, notes = subset_schema(schema, tables=keep)
    assert [v.name for v in subset.views] == ["vw_EmployeeDetails"]
    assert any("vw_ProjectSummary" in n and "Projects" in n for n in notes)


def test_a_view_that_only_mentions_the_word_is_kept():
    """The reference scan looks at table positions (FROM/JOIN/INTO/UPDATE)
    only, so a column or alias sharing a table's name is not a
    dependency."""
    schema = _schema()
    schema.views.append(View(
        name="vw_Coincidence", schema="dbo", source_engine="SQL Server",
        definition="SELECT ProjectID AS Projects FROM Employees"))
    keep = [t for t in schema.tables if t.name != "Projects"]
    subset, _notes = subset_schema(schema, tables=keep)
    assert "vw_Coincidence" in [v.name for v in subset.views]


def test_unticking_views_sequences_and_routines():
    schema = _schema()
    subset, _notes = subset_schema(
        schema, views=[], sequences=[], routines=[schema.routines[0]])
    assert subset.views == [] and subset.sequences == []
    assert [r.name for r in subset.routines] == ["usp_GetEmployees"]
    ddl, _issues = generate_schema_ddl(subset, "MySQL")
    assert "vw_EmployeeDetails" not in ddl


def test_kept_objects_are_the_same_instances():
    """So a conversion writes its status straight onto the objects the
    tree is displaying."""
    schema = _schema()
    subset, _notes = subset_schema(schema)
    assert subset.tables[0] is schema.tables[0]
    assert subset.views[0] is schema.views[0]
    assert subset.routines[0] is schema.routines[0]


def test_sync_conversion_results_carries_a_trimmed_table_status_back():
    from tgdatabridge.core.schema_model import ConversionStatus

    schema = _schema()
    keep = [t for t in schema.tables if t.name != "Employees"]
    subset, _notes = subset_schema(schema, tables=keep)
    trimmed = next(t for t in subset.tables if t.name == "EmployeeAttendance")
    assert trimmed is not next(t for t in schema.tables if t.name == "EmployeeAttendance")
    trimmed.status = ConversionStatus.AUTOMATIC_WITH_WARNINGS
    sync_conversion_results(subset, schema)
    original = next(t for t in schema.tables if t.name == "EmployeeAttendance")
    assert original.status is ConversionStatus.AUTOMATIC_WITH_WARNINGS


# ------------------------------------------------------- the tree widget

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def tree(qt_app):
    from tgdatabridge.gui.schema_tree import SchemaTree

    widget = SchemaTree()
    widget.load_schema(_schema(), converted=False)
    return widget


def test_everything_starts_ticked(tree):
    assert len(tree.checked_objects("Tables")) == 3
    assert tree.excluded_summary() == []


def test_the_selection_survives_the_reload_after_a_conversion(tree):
    schema = _schema()
    tree.load_schema(schema, converted=False, preserve_selection=False)
    from PySide6.QtCore import Qt

    tables = tree._category_items["Tables"]
    for i in range(tables.childCount()):
        if tables.child(i).text(0) == "Projects":
            tables.child(i).setCheckState(0, Qt.CheckState.Unchecked)
    assert [t.name for t in tree.checked_objects("Tables")] == [
        "Employees", "EmployeeAttendance"]

    # This is the rebuild that used to silently re-tick Projects.
    tree.load_schema(schema, converted=True)
    assert [t.name for t in tree.checked_objects("Tables")] == [
        "Employees", "EmployeeAttendance"]
    assert any("Projects" in line for line in tree.excluded_summary())


def test_a_fresh_load_starts_fully_ticked_again(tree):
    from PySide6.QtCore import Qt

    tables = tree._category_items["Tables"]
    tables.child(0).setCheckState(0, Qt.CheckState.Unchecked)
    tree.load_schema(_schema(), converted=False, preserve_selection=False)
    assert len(tree.checked_objects("Tables")) == 3


def test_select_all_clear_all_and_invert(tree):
    tree.set_all_checked(False)
    assert tree.checked_objects("Tables") == []
    assert not tree.has_any_checked()
    tree.set_all_checked(True)
    assert len(tree.checked_objects("Tables")) == 3
    assert tree.has_any_checked()
    tree.invert_selection("Tables")
    assert tree.checked_objects("Tables") == []
    assert tree.has_any_checked(), "other categories are still ticked"


def test_the_category_label_shows_the_count(tree):
    from PySide6.QtCore import Qt

    assert tree._category_items["Tables"].text(0) == "Tables (3)"
    tree._category_items["Tables"].child(0).setCheckState(0, Qt.CheckState.Unchecked)
    assert tree._category_items["Tables"].text(0) == "Tables (2 of 3 selected)"


# ------------------------------------------------ the window, end to end

def test_the_window_converts_only_what_is_ticked(qt_app):
    """The wiring itself: tick state -> _selected_schema -> the tables the
    data steps will touch."""
    from PySide6.QtCore import Qt

    from tgdatabridge.gui.main_window import MainWindow

    window = MainWindow()
    schema = _schema()
    window.schema = schema
    window.schema_tree.load_schema(schema, converted=False, preserve_selection=False)
    window._update_selection_label()
    assert window.selection_label.text() == "All objects selected"

    tables = window.schema_tree._category_items["Tables"]
    for i in range(tables.childCount()):
        if tables.child(i).text(0) == "Projects":
            tables.child(i).setCheckState(0, Qt.CheckState.Unchecked)
    window._update_selection_label()
    assert "1 object(s) left out" in window.selection_label.text()

    subset, notes = window._selected_schema()
    assert [t.name for t in subset.tables] == ["Employees", "EmployeeAttendance"]
    assert any("vw_ProjectSummary" in n for n in notes)
    assert [v.name for v in subset.views] == ["vw_EmployeeDetails"]
    assert [t.name for t in window._migration_tables()] == [
        "Employees", "EmployeeAttendance"]

    window.schema_tree.set_all_checked(False)
    assert window._selected_schema() == (None, [])
    window.close()
