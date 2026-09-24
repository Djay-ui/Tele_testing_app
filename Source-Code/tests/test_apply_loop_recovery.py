"""The apply loop must finish the script, not stop four statements short.

This drives the real `_apply_ddl_text` -- the same closure the GUI runs --
with the worker made synchronous, against a fake target that refuses one
foreign key the way PostgreSQL does. What is being checked is not the SQL
(tests/test_fk_recovery.py does that) but the loop's behaviour around it:
that the statements after the failure still run, that the constraint ends
up on the target, and that the user is told rather than left guessing.
"""
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 installed")

from PySide6.QtWidgets import QApplication, QMessageBox   # noqa: E402

from tgdatabridge.core import fk_recovery as R                   # noqa: E402
from tgdatabridge.db.base import ConnectionParams                # noqa: E402
from tgdatabridge.gui import main_window as MW                   # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


class _Violation(Exception):
    """Shaped like psycopg's, down to the sqlstate the code keys off."""
    sqlstate = "23503"

    def __str__(self):
        return ('insert or update on table "team_members" violates foreign key '
                'constraint "team_members_team_id_foreign"\nDETAIL:  Key '
                '(team_id)=(7f304fcf) is not present in table "teams".')


class PostgresConnector:
    """Refuses the plain ADD CONSTRAINT, accepts the NOT VALID form --
    exactly what a real PostgreSQL does when the rows break the key."""

    def __init__(self, *_a, **_kw):
        self.applied = []
        self.closed = False

    def connect(self):
        return None

    def close(self):
        self.closed = True

    def execute_ddl(self, sql):
        flat = " ".join(sql.split())
        if "ADD CONSTRAINT" in flat and "FOREIGN KEY" in flat and "NOT VALID" not in flat:
            raise _Violation()
        self.applied.append(flat)

    def execute(self, sql, params=None):
        flat = " ".join(sql.split()).upper()
        if "COUNT(*)" in flat and "LEFT JOIN" in flat:
            return [(1,)]
        if '"TEAMS"' in flat:
            return [(217,)]
        return [(692,)]


SCRIPT = "\n".join([
    'CREATE TABLE "teams" ("id" CHAR(36));',
    'CREATE TABLE "team_members" ("id" CHAR(36), "team_id" CHAR(36));',
    'ALTER TABLE "team_members" ADD CONSTRAINT "team_members_team_id_foreign" '
    'FOREIGN KEY ("team_id") REFERENCES "teams" ("id");',
    'CREATE INDEX "ix_after_the_failure" ON "team_members" ("team_id");',
    'CREATE INDEX "ix_also_after" ON "teams" ("id");',
])


@pytest.fixture
def window(qt_app, monkeypatch):
    win = MW.MainWindow()
    win.target_params = ConnectionParams("h", 5432, "d", "u", "")
    win.schema = None
    win.active_schema = None

    target = PostgresConnector()
    monkeypatch.setattr(MW, "_make_target_connector", lambda engine, params: target)
    monkeypatch.setattr(MW.MainWindow, "_refresh_target_schema",
                        lambda self, silent=False: None)

    shown = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[-1])))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a[-1])))

    def run_now(self, label, fn, on_success, *args, **kwargs):
        on_success(fn(*args))

    monkeypatch.setattr(MW.MainWindow, "_run_async", run_now)

    win._target = target
    win._shown = shown
    return win


def test_the_statements_after_the_refused_key_still_run(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    applied = window._target.applied
    assert any("ix_after_the_failure" in s for s in applied)
    assert any("ix_also_after" in s for s in applied)


def test_the_constraint_ends_up_on_the_target(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    created = [s for s in window._target.applied if "ADD CONSTRAINT" in s]
    assert len(created) == 1
    assert created[0].endswith("NOT VALID;")


def test_nothing_is_deleted_or_blanked_by_the_default_policy(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    assert not any(s.upper().startswith(("DELETE", "UPDATE"))
                   for s in window._target.applied)


def test_the_user_is_told_what_happened_and_how_many_rows(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    text = "\n".join(window._shown)
    assert "team_members_team_id_foreign" in text
    assert "1 orphan row" in text
    assert "FOREIGN_KEY_CHECKS" in text          # where the orphans came from
    assert "VALIDATE CONSTRAINT" in text          # how to finish the job later


def test_the_run_is_reported_as_applied_not_as_failed(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    assert any("Successfully applied" in t for t in window._shown)


def test_stop_and_ask_still_stops(window, monkeypatch):
    """The old behaviour has to remain available -- some people want to
    look at every one."""
    monkeypatch.setattr(MW.MainWindow, "_fk_policy", lambda self: R.POLICY_STOP)
    asked = []
    monkeypatch.setattr(MW.MainWindow, "_offer_continue_past_error",
                        lambda self, failure, *a: asked.append(failure))
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    assert len(asked) == 1
    assert "team_members" in asked[0].explanation
    assert not any("ix_after_the_failure" in s for s in window._target.applied)


def test_the_connection_is_closed_either_way(window):
    window._apply_ddl_text(SCRIPT, "Apply Post-Load DDL", "nothing to apply")
    assert window._target.closed is True
