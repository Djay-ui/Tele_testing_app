"""Being blocked by a table already on the target must offer a way out.

The pre-flight check is right to stop -- CREATE TABLE IF NOT EXISTS over a
table with different columns silently does nothing, and the run then fails
several statements later on a foreign key with the schema half-applied.
But an error dialog with only an OK button leaves the user to fix it in
another tool, and both of the things they want are things this one can do.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtCore import Qt                       # noqa: E402
from PySide6.QtWidgets import QApplication          # noqa: E402

from tgdatabridge.core.schema_model import Column, Constraint, Schema, Table, View  # noqa: E402
from tgdatabridge.core.target_shape import ShapeProblem    # noqa: E402
from tgdatabridge.gui.main_window import MainWindow, _PreflightBlocked  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _schema():
    schema = Schema(name="public", source_engine="MySQL", target_engine="PostgreSQL")
    accounts = Table(name="accounts", schema="public", columns=[
        Column(name="account_id", data_type="VARCHAR2(36)", nullable=False),
    ], constraints=[
        Constraint(name="pk_accounts", kind="PRIMARY KEY", columns=["account_id"]),
    ])
    contacts = Table(name="contacts", schema="public", columns=[
        Column(name="contact_id", data_type="VARCHAR2(36)", nullable=False),
        Column(name="account_id", data_type="VARCHAR2(36)"),
    ], constraints=[
        Constraint(name="fk_contacts_accounts", kind="FOREIGN KEY",
                   columns=["account_id"], ref_table="accounts",
                   ref_columns=["account_id"]),
    ])
    workflows = Table(name="workflows", schema="public", columns=[
        Column(name="id", data_type="VARCHAR2(36)", nullable=False),
    ])
    schema.tables = [accounts, contacts, workflows]
    schema.views = [View(name="vw_accounts", schema="public", source_engine="MySQL",
                         definition="SELECT account_id FROM accounts")]
    return schema


@pytest.fixture
def window(qt_app):
    win = MainWindow()
    schema = _schema()
    win.schema = schema
    win.active_schema = schema
    win.schema_tree.load_schema(schema, converted=False, preserve_selection=False)
    yield win
    win.close()


def test_the_block_is_carried_back_not_raised():
    """A raised error is a dead end; a returned one can be offered
    choices."""
    blocked = _PreflightBlocked([ShapeProblem(table_name="accounts",
                                              missing_columns=["atd_localName"])])
    assert blocked.table_names == ["accounts"]


def test_leaving_them_out_unticks_exactly_those_tables(window, monkeypatch):
    monkeypatch.setattr(
        "tgdatabridge.gui.main_window.QMessageBox.information", lambda *a, **k: None)
    window._untick_tables(["accounts"])
    still_ticked = [t.name for t in window.schema_tree.checked_objects("Tables")]
    assert still_ticked == ["contacts", "workflows"]


def test_leaving_them_out_also_drops_what_depended_on_them(window, monkeypatch):
    """The point of reusing the checkboxes: the subset machinery already
    removes a foreign key into an excluded table and a view that selects
    from one, so the rest of the script actually applies."""
    monkeypatch.setattr(
        "tgdatabridge.gui.main_window.QMessageBox.information", lambda *a, **k: None)
    window._untick_tables(["accounts"])
    subset, notes = window._selected_schema()
    assert [t.name for t in subset.tables] == ["contacts", "workflows"]
    contacts = subset.tables[0]
    assert [c.name for c in contacts.constraints] == []
    assert [v.name for v in subset.views] == []
    assert any("fk_contacts_accounts" in n for n in notes)
    assert any("vw_accounts" in n for n in notes)


def test_the_case_of_the_name_does_not_matter(window, monkeypatch):
    """PostgreSQL folds to lower case and MySQL on Windows is
    case-insensitive, so the name the target reports may not be spelled
    the way the source spells it."""
    monkeypatch.setattr(
        "tgdatabridge.gui.main_window.QMessageBox.information", lambda *a, **k: None)
    window._untick_tables(["ACCOUNTS"])
    assert [t.name for t in window.schema_tree.checked_objects("Tables")] == [
        "contacts", "workflows"]


def test_replacing_them_drops_only_the_conflicting_tables(window, monkeypatch):
    """Not the whole rollback script: "Reset Target…" exists for wiping
    the loaded schema, and using it here would drop objects that were
    never in the way."""
    captured = {}

    def fake_apply(script, label, empty_hint, **kwargs):
        captured["script"] = script
        captured["label"] = label
        captured["kwargs"] = kwargs

    monkeypatch.setattr(window, "_apply_ddl_text", fake_apply)
    monkeypatch.setattr(
        "tgdatabridge.gui.main_window.QMessageBox.exec",
        lambda self: __import__("PySide6.QtWidgets", fromlist=["QMessageBox"])
        .QMessageBox.StandardButton.Yes)
    window._replace_target_tables(
        ["accounts"], "CREATE TABLE ...;", "Apply DDL to Target", "hint", "PostgreSQL")
    script = captured["script"]
    assert "accounts" in script
    assert "contacts" not in script and "workflows" not in script
    assert captured["kwargs"]["preflight"] is False
    assert captured["kwargs"]["continue_on_error"] is True
    assert captured["kwargs"]["then"] is not None, "the apply has to follow the drop"


def test_replacing_is_cancellable(window, monkeypatch):
    called = []
    monkeypatch.setattr(window, "_apply_ddl_text",
                        lambda *a, **k: called.append(a))
    monkeypatch.setattr(
        "tgdatabridge.gui.main_window.QMessageBox.exec",
        lambda self: __import__("PySide6.QtWidgets", fromlist=["QMessageBox"])
        .QMessageBox.StandardButton.Cancel)
    window._replace_target_tables(
        ["accounts"], "CREATE TABLE ...;", "Apply DDL to Target", "hint", "PostgreSQL")
    assert called == [], "nothing may be dropped without an explicit yes"


def test_replacing_a_table_the_tool_does_not_know_is_refused(window, monkeypatch):
    warned = []
    monkeypatch.setattr("tgdatabridge.gui.main_window.QMessageBox.warning",
                        lambda *a, **k: warned.append(a))
    monkeypatch.setattr(window, "_apply_ddl_text",
                        lambda *a, **k: pytest.fail("must not apply anything"))
    window._replace_target_tables(
        ["something_else"], "CREATE TABLE ...;", "Apply DDL to Target", "hint",
        "PostgreSQL")
    assert warned, "there is no definition to drop it by -- say so"


# ------------------------------------- carrying on past a failed statement

def test_a_failed_statement_is_carried_back_not_raised():
    from tgdatabridge.gui.main_window import _ApplyFailed

    failure = _ApplyFailed(117, 1061, 'syntax error at or near "00"',
                           'CREATE TABLE IF NOT EXISTS "autodesk_opportunities" (...')
    assert failure.index == 117 and failure.total == 1061


def test_continuing_re_runs_the_whole_script_collecting_every_failure(window, monkeypatch):
    """One class of problem across thirty tables used to mean thirty runs
    and thirty dialogs. Safe to offer because the script is idempotent --
    what already exists is skipped, not re-applied."""
    from PySide6.QtWidgets import QMessageBox

    from tgdatabridge.gui.main_window import _ApplyFailed

    captured = {}
    monkeypatch.setattr(window, "_apply_ddl_text",
                        lambda *a, **k: captured.update(args=a, kwargs=k))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: self.buttons()[0])  # the "Apply the rest" button

    window._offer_continue_past_error(
        _ApplyFailed(117, 1061, "syntax error", "CREATE TABLE ..."),
        "CREATE TABLE ...;", "Apply DDL to Target", "hint")
    assert captured["kwargs"]["continue_on_error"] is True
    assert captured["kwargs"]["preflight"] is False


def test_stopping_does_nothing_further(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from tgdatabridge.gui.main_window import _ApplyFailed

    monkeypatch.setattr(window, "_apply_ddl_text",
                        lambda *a, **k: pytest.fail("must not re-run"))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: self.buttons()[1])  # "Stop"
    window._offer_continue_past_error(
        _ApplyFailed(117, 1061, "syntax error", "CREATE TABLE ..."),
        "CREATE TABLE ...;", "Apply DDL to Target", "hint")
