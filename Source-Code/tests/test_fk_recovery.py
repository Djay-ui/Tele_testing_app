"""A foreign key whose rows break it must not end the migration.

The scenario these guard is the one that actually happened: a 1,572
statement post-load script, one `team_members` row out of 692 pointing at
a `teams` row that had been deleted years earlier in MySQL, and four
statements after it that never ran.
"""
from __future__ import annotations

import pytest

from tgdatabridge.core import fk_recovery as R
from tgdatabridge.core.fk_violations import ForeignKey, parse_add_foreign_key

PG_STATEMENT = (
    'ALTER TABLE "team_members" ADD CONSTRAINT "team_members_team_id_foreign" '
    'FOREIGN KEY ("team_id") REFERENCES "teams" ("id")')

#: How the generator actually emits it -- wrapped in the DO block that
#: stands in for PostgreSQL's missing ADD CONSTRAINT IF NOT EXISTS. Any
#: rewriting has to survive this, which is why the statement is rebuilt
#: from the parse rather than string-patched.
PG_WRAPPED = (
    'DO $$ BEGIN\n'
    '  ALTER TABLE "team_members" ADD CONSTRAINT "team_members_team_id_foreign" '
    'FOREIGN KEY ("team_id") REFERENCES "teams" ("id");\n'
    'EXCEPTION\n  WHEN duplicate_object THEN NULL;\nEND $$;')


# --------------------------------------------------------------- doubles

class PostgresConnector:
    """Enough of a connector to drive fk_recovery: it records the DDL it
    is given and answers counting queries from a dict."""

    def __init__(self, counts=None, fail_on=None):
        self.ddl = []
        self.counts = counts or {}
        self.fail_on = fail_on or ()

    def execute_ddl(self, sql):
        for needle in self.fail_on:
            if needle.lower() in sql.lower():
                raise RuntimeError(f"refused: {needle}")
        self.ddl.append(sql)

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        for needle, value in self.counts.items():
            if needle.lower() in flat.lower():
                return [(value,)]
        return [(0,)]


class MySQLConnector(PostgresConnector):
    pass


class SQLServerConnector(PostgresConnector):
    pass


class OracleConnector(PostgresConnector):
    pass


class Db2Connector(PostgresConnector):
    pass


class MongoConnector(PostgresConnector):
    pass


FK = ForeignKey(
    constraint="team_members_team_id_foreign",
    child_table="team_members", child_columns=["team_id"],
    parent_table="teams", parent_columns=["id"],
    child_table_sql='"team_members"', parent_table_sql='"teams"')


# --------------------------------------------------------------- dialect

def test_dialect_is_read_from_the_connector_class():
    assert R.dialect_of(PostgresConnector()) == "postgres"
    assert R.dialect_of(MySQLConnector()) == "mysql"
    assert R.dialect_of(SQLServerConnector()) == "sqlserver"
    assert R.dialect_of(OracleConnector()) == "oracle"
    assert R.dialect_of(Db2Connector()) == "db2"
    assert R.dialect_of(MongoConnector()) == "mongo"


def test_an_unknown_connector_is_treated_as_standard_sql():
    class SomethingElse:
        pass
    assert R.dialect_of(SomethingElse()) == "postgres"


def test_mongo_has_no_foreign_keys_to_create():
    assert R.supports_unvalidated(MongoConnector()) is False
    assert R.supports_unvalidated(PostgresConnector()) is True


# ------------------------------------------------- the unvalidated forms

def test_postgres_uses_not_valid():
    sql = R.unvalidated_statements(PostgresConnector(), FK)
    assert len(sql) == 1
    assert sql[0].endswith('NOT VALID;')
    assert 'ALTER TABLE "team_members" ADD CONSTRAINT' in sql[0]


def test_sqlserver_puts_nocheck_before_add_not_after_references():
    sql = R.unvalidated_statements(SQLServerConnector(), FK)[0]
    assert "WITH NOCHECK ADD CONSTRAINT" in sql
    assert not sql.rstrip(";").endswith("NOCHECK")


def test_oracle_uses_enable_novalidate():
    assert R.unvalidated_statements(OracleConnector(), FK)[0].endswith("ENABLE NOVALIDATE")


def test_db2_uses_not_enforced():
    assert R.unvalidated_statements(Db2Connector(), FK)[0].endswith("NOT ENFORCED")


def test_mysql_brackets_the_alter_with_the_checks_off():
    sql = R.unvalidated_statements(MySQLConnector(), FK)
    assert len(sql) == 3
    assert sql[0] == "SET FOREIGN_KEY_CHECKS = 0;"
    assert sql[2] == "SET FOREIGN_KEY_CHECKS = 1;"
    assert "ALTER TABLE `team_members`" in sql[1]
    assert "NOT VALID" not in sql[1]  # MySQL has no such clause


def test_referential_actions_survive_the_rebuild():
    """Rebuilding from the parse must not quietly drop ON DELETE CASCADE."""
    fk = parse_add_foreign_key(
        'ALTER TABLE "a" ADD CONSTRAINT "fk" FOREIGN KEY ("b") '
        'REFERENCES "c" ("d") ON DELETE CASCADE ON UPDATE SET NULL')
    assert fk.actions == "ON DELETE CASCADE ON UPDATE SET NULL"
    sql = R.unvalidated_statements(PostgresConnector(), fk)[0]
    assert "ON DELETE CASCADE ON UPDATE SET NULL NOT VALID;" in sql


def test_a_composite_key_keeps_its_column_order():
    fk = parse_add_foreign_key(
        'ALTER TABLE "a" ADD CONSTRAINT "fk" FOREIGN KEY ("x", "y") '
        'REFERENCES "c" ("p", "q")')
    sql = R.unvalidated_statements(PostgresConnector(), fk)[0]
    assert '("x", "y")' in sql and '("p", "q")' in sql


def test_a_schema_qualified_table_stays_qualified():
    """Re-quoting the bare name would resolve against search_path instead
    of the table that actually failed."""
    fk = parse_add_foreign_key(
        'ALTER TABLE "app"."team_members" ADD CONSTRAINT "fk" '
        'FOREIGN KEY ("team_id") REFERENCES "app"."teams" ("id")')
    assert fk.child_table == "team_members"      # what the user reads
    assert fk.child_table_sql == '"app"."team_members"'  # what SQL uses
    sql = R.unvalidated_statements(PostgresConnector(), fk)[0]
    assert 'ALTER TABLE "app"."team_members"' in sql
    assert 'REFERENCES "app"."teams"' in sql


def test_the_do_block_wrapper_does_not_end_up_with_not_valid_after_end():
    sql = R.unvalidated_statements(PostgresConnector(),
                                   parse_add_foreign_key(PG_WRAPPED))[0]
    assert "DO $$" not in sql
    assert "EXCEPTION" not in sql
    assert sql.strip().endswith("NOT VALID;")


# ------------------------------------------------------ validate / clean

def test_the_promote_statement_is_offered_per_engine():
    assert R.validate_statement(PostgresConnector(), FK) == (
        'ALTER TABLE "team_members" VALIDATE CONSTRAINT "team_members_team_id_foreign";')
    assert "ENABLE VALIDATE" in R.validate_statement(OracleConnector(), FK)
    assert "WITH CHECK CHECK CONSTRAINT" in R.validate_statement(SQLServerConnector(), FK)
    assert "ENFORCED" in R.validate_statement(Db2Connector(), FK)
    # MySQL's constraint is already fully enforcing -- nothing to promote.
    assert R.validate_statement(MySQLConnector(), FK) is None


def test_blanking_orphans_uses_not_exists_everywhere_but_mysql():
    sql = R.null_orphans_statement(PostgresConnector(), FK)
    assert sql.startswith('UPDATE "team_members" SET "team_id" = NULL')
    assert "NOT EXISTS" in sql
    assert '"team_id" IS NOT NULL' in sql


def test_blanking_orphans_uses_a_join_on_mysql():
    """MySQL error 1093 forbids reading the table an UPDATE is writing."""
    sql = R.null_orphans_statement(MySQLConnector(), FK)
    assert "LEFT JOIN" in sql
    assert "NOT EXISTS" not in sql
    assert sql.startswith("UPDATE `team_members` c LEFT JOIN `teams` p")


def test_deleting_orphans_uses_a_join_on_mysql_and_not_exists_elsewhere():
    assert R.delete_orphans_statement(MySQLConnector(), FK).startswith("DELETE c FROM")
    pg = R.delete_orphans_statement(PostgresConnector(), FK)
    assert pg.startswith('DELETE FROM "team_members"')
    assert "NOT EXISTS" in pg


def test_neither_cleanup_touches_rows_whose_key_is_null():
    """A NULL foreign key is not a violation on any engine; blanking or
    deleting those rows would be destroying data for no reason."""
    for build in (R.null_orphans_statement, R.delete_orphans_statement):
        for conn in (PostgresConnector(), MySQLConnector()):
            assert "IS NOT NULL" in build(conn, FK)


# --------------------------------------------------------------- recover

def test_the_default_policy_creates_the_key_and_changes_no_data():
    target = PostgresConnector(counts={"COUNT(*) FROM \"team_members\" c": 1,
                                       'COUNT(*) FROM "team_members"': 692,
                                       'COUNT(*) FROM "teams"': 217})
    done = R.recover(target, PG_WRAPPED)
    assert done.ok and done.applied == R.POLICY_NOT_VALID
    assert len(target.ddl) == 1 and target.ddl[0].endswith("NOT VALID;")
    assert "UPDATE" not in target.ddl[0] and "DELETE" not in target.ddl[0]


def test_stop_returns_nothing_so_the_caller_reports_the_failure():
    assert R.recover(PostgresConnector(), PG_STATEMENT, R.POLICY_STOP) is None


def test_skip_creates_nothing():
    target = PostgresConnector()
    done = R.recover(target, PG_STATEMENT, R.POLICY_SKIP)
    assert done.ok and done.applied == R.POLICY_SKIP
    assert target.ddl == []


def test_a_statement_that_is_not_an_add_foreign_key_is_not_ours():
    assert R.recover(PostgresConnector(), 'CREATE TABLE "x" ("a" INT)') is None
    assert R.recover(PostgresConnector(), "") is None


def test_a_mismatched_column_count_is_refused_rather_than_guessed():
    assert R.recover(PostgresConnector(),
                     'ALTER TABLE "a" ADD CONSTRAINT "f" FOREIGN KEY ("x", "y") '
                     'REFERENCES "c" ("p")') is None


def test_blanking_runs_the_update_then_creates_the_key_fully_checked():
    target = PostgresConnector()
    done = R.recover(target, PG_STATEMENT, R.POLICY_NULL_ORPHANS)
    assert done.ok and done.applied == R.POLICY_NULL_ORPHANS
    assert target.ddl[0].startswith("UPDATE")
    assert "NOT VALID" not in target.ddl[-1]
    assert "ADD CONSTRAINT" in target.ddl[-1]


def test_deleting_runs_the_delete_then_creates_the_key_fully_checked():
    target = PostgresConnector()
    done = R.recover(target, PG_STATEMENT, R.POLICY_DELETE_ORPHANS)
    assert done.ok and done.applied == R.POLICY_DELETE_ORPHANS
    assert target.ddl[0].startswith("DELETE")
    assert "NOT VALID" not in target.ddl[-1]


def test_a_not_null_column_falls_back_to_the_unvalidated_form():
    """Blanking cannot work when the key column is NOT NULL. Leaving the
    constraint off would be worse than creating it unvalidated, so it
    falls through rather than giving up."""
    target = PostgresConnector(fail_on=("UPDATE",))
    done = R.recover(target, PG_STATEMENT, R.POLICY_NULL_ORPHANS)
    assert done.ok and done.applied == R.POLICY_NOT_VALID
    assert any("could not blank" in n for n in done.notes)
    assert target.ddl[-1].endswith("NOT VALID;")


def test_a_recovery_that_is_itself_refused_reports_instead_of_raising():
    target = PostgresConnector(fail_on=("NOT VALID",))
    done = R.recover(target, PG_STATEMENT)
    assert done is not None and done.ok is False
    assert "refused" in done.error


def test_mysql_restores_foreign_key_checks_even_when_the_alter_fails():
    """Leaving the session with FOREIGN_KEY_CHECKS off would let bad rows
    into every table loaded afterwards -- far worse than the constraint
    this was trying to add."""
    target = MySQLConnector(fail_on=("ALTER TABLE",))
    done = R.recover(target, PG_STATEMENT)
    assert done.ok is False
    assert target.ddl[-1] == "SET FOREIGN_KEY_CHECKS = 1;"


def test_mongo_is_recorded_as_skipped_rather_than_failed():
    done = R.recover(MongoConnector(), PG_STATEMENT)
    assert done.ok and done.applied == R.POLICY_SKIP


def test_recover_never_raises_even_when_the_connector_does():
    class Hostile:
        def execute_ddl(self, sql):
            raise RuntimeError("boom")

        def execute(self, sql, params=None):
            raise RuntimeError("boom")

    done = R.recover(Hostile(), PG_STATEMENT)
    assert done is not None and done.ok is False


# --------------------------------------------------------------- reports

def _recovery(**kw):
    base = dict(fk=FK, policy=R.POLICY_NOT_VALID, applied=R.POLICY_NOT_VALID,
                ok=True, orphan_rows=1, child_rows=692, parent_rows=217)
    base.update(kw)
    return R.Recovery(**base)


def test_the_summary_line_names_the_tables_and_the_orphan_count():
    line = _recovery().one_line()
    assert "team_members_team_id_foreign" in line
    assert "team_members -> teams" in line
    assert "1 orphan row" in line
    assert "no data changed" in line


def test_an_empty_parent_is_called_out_because_it_means_something_worse():
    line = _recovery(parent_rows=0, parent_looks_empty=True).one_line()
    assert "EMPTY" in line and "check it migrated" in line
    text = R.summarise([_recovery(parent_rows=0, parent_looks_empty=True)])
    assert "did not migrate" in text


def test_the_summary_tells_the_user_the_key_is_really_enforced():
    text = R.summarise([_recovery()])
    assert "enforced from now on" in text
    assert "checked against it" in text


def test_the_summary_offers_the_promote_statement():
    text = R.summarise([_recovery(validate_sql='ALTER TABLE "t" VALIDATE CONSTRAINT "c";')])
    assert "VALIDATE CONSTRAINT" in text


def test_the_summary_explains_where_the_orphans_came_from():
    text = R.summarise([_recovery()])
    assert "FOREIGN_KEY_CHECKS" in text
    assert "InnoDB" in text


def test_no_recoveries_means_no_summary_at_all():
    assert R.summarise([]) == ""


def test_failed_and_skipped_recoveries_are_reported_separately():
    text = R.summarise([
        _recovery(),
        _recovery(applied=R.POLICY_SKIP),
        _recovery(ok=False, error="refused"),
    ])
    assert "1 was created anyway and is enforced from now on" in text
    assert "1 was left off" in text
    assert "1 could not be created at all" in text


def test_the_summary_reads_correctly_in_the_plural_too():
    text = R.summarise([_recovery(), _recovery(), _recovery(applied=R.POLICY_SKIP)])
    assert "3 foreign keys could not be created" in text
    assert "break them" in text
    assert "2 were created anyway and are enforced from now on" in text


def test_every_policy_has_a_label_for_the_dialog():
    for policy in R.POLICIES:
        assert R.POLICY_LABELS[policy]


@pytest.mark.parametrize("rows,expected", [(0, "0 row(s) blanked"), (5, "5 row(s) blanked")])
def test_the_blanked_row_count_is_reported(rows, expected):
    line = _recovery(applied=R.POLICY_NULL_ORPHANS, rows_changed=rows).one_line()
    assert expected in line
