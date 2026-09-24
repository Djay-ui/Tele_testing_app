"""A foreign key the target refuses because the rows break it.

    insert or update on table "team_members" violates foreign key
    constraint "team_members_team_id_foreign"
    DETAIL: Key (team_id)=(7f304fcf-...) is not present in table "teams".

Nothing is wrong with the conversion here -- the constraint, the types
and the tables are all right, and the data does not satisfy it. The
driver's message says neither how many rows are involved nor what to do,
and cannot tell "a handful of orphans came with the data" apart from "the
parent table never migrated", which are very different problems.
"""
import pytest

from tgdatabridge.core.fk_violations import (diagnose, is_foreign_key_violation,
                                      orphan_query, parse_add_foreign_key)

STATEMENT = ('ALTER TABLE "team_members" ADD CONSTRAINT '
             '"team_members_team_id_foreign" FOREIGN KEY ("team_id") '
             'REFERENCES "teams" ("id")')


class _Err(Exception):
    def __init__(self, message, errno=None, sqlstate=None):
        super().__init__(message)
        if errno is not None:
            self.errno = errno
        if sqlstate is not None:
            self.sqlstate = sqlstate


# ------------------------------------------------------- recognising it

@pytest.mark.parametrize("exc", [
    _Err('insert or update on table "team_members" violates foreign key '
         'constraint "team_members_team_id_foreign"', sqlstate="23503"),
    _Err("Cannot add or update a child row: a foreign key constraint fails", errno=1452),
    _Err("ORA-02291: integrity constraint (HR.FK_X) violated - parent key not found"),
    _Err("The INSERT statement conflicted with the FOREIGN KEY constraint", errno=547),
])
def test_a_violation_is_recognised_on_every_engine(exc):
    assert is_foreign_key_violation(exc)


@pytest.mark.parametrize("exc", [
    _Err('relation "teams" does not exist', sqlstate="42P01"),
    _Err("there is no unique constraint matching given keys", sqlstate="42830"),
    _Err("1826 (HY000): Duplicate foreign key constraint name", errno=1826),
])
def test_other_foreign_key_problems_are_not_confused_with_it(exc):
    """A missing table and a missing unique constraint need completely
    different advice."""
    assert not is_foreign_key_violation(exc)


# --------------------------------------------------------- reading it

def test_the_four_names_come_out_of_the_statement():
    fk = parse_add_foreign_key(STATEMENT)
    assert fk.constraint == "team_members_team_id_foreign"
    assert fk.child_table == "team_members" and fk.child_columns == ["team_id"]
    assert fk.parent_table == "teams" and fk.parent_columns == ["id"]


def test_a_composite_key_is_read_whole():
    fk = parse_add_foreign_key(
        'ALTER TABLE `a` ADD CONSTRAINT `fk` FOREIGN KEY (`x`, `y`) '
        'REFERENCES `b` (`p`, `q`)')
    assert fk.child_columns == ["x", "y"] and fk.parent_columns == ["p", "q"]


def test_anything_else_is_left_to_the_driver():
    assert parse_add_foreign_key("CREATE TABLE t (id INT)") is None


# ------------------------------------------------------ the diagnosis

class PostgresConnector:
    """Answers the three counting queries the diagnosis runs. Named so
    the quoting style is picked the way it is in the real thing."""

    def __init__(self, orphans, child, parent):
        self.answers = [orphans, child, parent]
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return [(self.answers.pop(0),)]


_Target = PostgresConnector


def test_the_orphan_query_uses_a_left_join_so_nulls_are_not_orphans():
    """A NULL foreign key is not a violation on any engine; NOT IN would
    report it as one."""
    target = _Target(0, 0, 0)
    sql = orphan_query(target, parse_add_foreign_key(STATEMENT))
    assert "LEFT JOIN" in sql
    assert "IS NOT NULL" in sql
    assert '"team_members"' in sql and '"teams"' in sql


def test_a_handful_of_orphans_is_explained_as_data():
    found = diagnose(_Target(12, 4158, 300), STATEMENT)
    message = found.message()
    assert "12 of 4,158" in message
    assert "300 rows" in message
    assert "FOREIGN_KEY_CHECKS" in message, "say where orphans come from"
    assert "SELECT c.*" in message, "and hand over the query to find them"


def test_an_empty_parent_is_called_out_as_the_more_serious_case():
    """Every child row orphaned because the parent table never migrated
    is a different problem and needs different advice."""
    found = diagnose(_Target(4158, 4158, 0), STATEMENT)
    message = found.message()
    assert found.parent_looks_empty
    assert "EMPTY" in message
    assert "not having migrated" in message
    assert "FOREIGN_KEY_CHECKS" not in message


def test_a_diagnosis_that_cannot_run_never_blocks_the_report():
    class _BrokenPostgresConnector(PostgresConnector):
        def execute(self, sql, params=None):
            raise RuntimeError("permission denied for table teams")

    found = diagnose(_BrokenPostgresConnector(0, 0, 0), STATEMENT)
    assert found is not None
    assert "could not count them" in found.message()
    assert "permission denied" in found.message()


def test_a_statement_that_is_not_a_foreign_key_produces_nothing():
    assert diagnose(_Target(0, 0, 0), "CREATE TABLE t (id INT)") is None


def test_the_gui_shows_the_explanation_and_logs_it():
    import inspect

    from tgdatabridge.gui import main_window

    assert "explanation" in inspect.getsource(main_window._ApplyFailed)
    offer = inspect.getsource(main_window.MainWindow._offer_continue_past_error)
    assert "failure.explanation" in offer
    apply_path = inspect.getsource(main_window.MainWindow._apply_ddl_text)
    assert "is_foreign_key_violation" in apply_path
