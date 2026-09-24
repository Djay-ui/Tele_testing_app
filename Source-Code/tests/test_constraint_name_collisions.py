"""Every MySQL table's primary key is named `PRIMARY`.

That is legal on MySQL, which scopes constraint and index names to the
table. PostgreSQL, Oracle, SQL Server and Db2 scope them to the *schema*,
so a schema of 500 MySQL tables produces 500 constraints called PRIMARY
and only the first table can be created:

    CREATE TABLE "drop_down_lang" (... CONSTRAINT "primary" PRIMARY KEY ("id"))
    ERROR: relation "primary" already exists

and because that message says "already exists", the apply loop skipped it
as a table that was already there -- so the table silently never existed,
and the damage surfaced 1000 statements later as

    there is no unique constraint matching given keys for referenced
    table "dropdown_lists"
"""
import pytest

from tgdatabridge.core.ddl_generator import generate_schema_ddl, uniquify_object_names
from tgdatabridge.core.schema_model import Column, Constraint, Index, Schema, Table
from tgdatabridge.utils.ddl_errors import is_already_exists_error


def _schema(names=("dropdown_lists", "drop_down_lang", "accounts"),
            target="PostgreSQL", index_name="idx_ref"):
    schema = Schema(name="public", source_engine="MySQL", target_engine=target)
    for name in names:
        table = Table(name=name, schema="public", columns=[
            Column(name="id", data_type="NUMBER(10)", nullable=False, identity=True),
            Column(name="ref", data_type="NUMBER(10)"),
        ])
        table.constraints = [Constraint(name="PRIMARY", kind="PRIMARY KEY", columns=["id"])]
        table.indexes = [Index(name=index_name, columns=["ref"])]
        schema.tables.append(table)
    return schema


# ------------------------------------------------------------ the renaming

def test_every_primary_key_gets_its_own_name():
    schema = _schema()
    uniquify_object_names(schema, "PostgreSQL")
    names = [t.constraints[0].name for t in schema.tables]
    assert names == ["dropdown_lists_pkey", "drop_down_lang_pkey", "accounts_pkey"]
    assert len(set(names)) == len(names)


def test_repeated_index_names_are_separated_too():
    schema = _schema()
    uniquify_object_names(schema, "PostgreSQL")
    names = [t.indexes[0].name for t in schema.tables]
    assert names == ["idx_ref", "drop_down_lang_idx_ref", "accounts_idx_ref"]
    assert len(set(names)) == 3


def test_a_mysql_target_is_left_completely_alone():
    """These names are per-table on MySQL. Renaming them would change
    working DDL for no reason."""
    schema = _schema(target="MySQL")
    uniquify_object_names(schema, "MySQL")
    assert [t.constraints[0].name for t in schema.tables] == ["PRIMARY"] * 3
    assert [t.indexes[0].name for t in schema.tables] == ["idx_ref"] * 3


@pytest.mark.parametrize("target", ["PostgreSQL", "Oracle", "SQL Server", "DB2"])
def test_every_schema_scoped_engine_is_covered(target):
    schema = _schema(target=target)
    uniquify_object_names(schema, target)
    names = [t.constraints[0].name for t in schema.tables]
    assert len(set(names)) == 3


def test_renaming_is_idempotent():
    """A re-converted schema must produce the script that was already
    applied, or re-applying would create a second set of constraints."""
    schema = _schema()
    uniquify_object_names(schema, "PostgreSQL")
    first = [(t.constraints[0].name, t.indexes[0].name) for t in schema.tables]
    uniquify_object_names(schema, "PostgreSQL")
    uniquify_object_names(schema, "PostgreSQL")
    assert [(t.constraints[0].name, t.indexes[0].name) for t in schema.tables] == first


def test_oracles_shorter_identifier_limit_is_respected():
    long_name = "a_very_long_table_name_that_goes_past_thirty_characters"
    schema = _schema(names=(long_name, long_name + "_two"), target="Oracle")
    uniquify_object_names(schema, "Oracle")
    names = [t.constraints[0].name for t in schema.tables]
    assert all(len(n) <= 30 for n in names), names
    assert len(set(names)) == 2, "truncation must not collapse two names into one"


def test_the_renaming_is_reported():
    schema = _schema()
    issues = uniquify_object_names(schema, "PostgreSQL")
    assert issues and "unique" in issues[0].message
    assert "PRIMARY" in issues[0].message


# ------------------------------------------------- the generated script

def test_the_generated_script_has_no_duplicate_constraint_names():
    ddl, _issues = generate_schema_ddl(_schema(), "PostgreSQL", "public")
    assert 'CONSTRAINT "primary"' not in ddl
    for name in ("dropdown_lists_pkey", "drop_down_lang_pkey", "accounts_pkey"):
        assert ddl.count(f'CONSTRAINT "{name}"') == 1


def test_the_foreign_key_still_points_at_the_right_table():
    """Renaming the constraint must not touch what it references."""
    schema = _schema()
    schema.tables[1].constraints.append(Constraint(
        name="drop_down_lang_dropdown_list_id_foreign", kind="FOREIGN KEY",
        columns=["ref"], ref_table="dropdown_lists", ref_columns=["id"]))
    ddl, _issues = generate_schema_ddl(schema, "PostgreSQL", "public")
    assert 'REFERENCES "dropdown_lists" ("id")' in ddl
    assert 'CONSTRAINT "dropdown_lists_pkey" PRIMARY KEY ("id")' in ddl


# --------------------------------------- the error that hid the failure

def test_a_collision_on_another_object_is_not_read_as_already_exists():
    statement = ('CREATE TABLE IF NOT EXISTS "drop_down_lang" ("id" BIGINT, '
                 'CONSTRAINT "primary" PRIMARY KEY ("id"))')
    assert not is_already_exists_error(
        Exception('relation "primary" already exists'), statement)


def test_the_same_table_already_existing_is_still_a_skip():
    statement = 'CREATE TABLE IF NOT EXISTS "drop_down_lang" ("id" BIGINT)'
    assert is_already_exists_error(
        Exception('relation "drop_down_lang" already exists'), statement)


def test_a_duplicate_foreign_key_is_still_a_skip():
    statement = ('ALTER TABLE "t" ADD CONSTRAINT "fk_x" FOREIGN KEY ("a") '
                 'REFERENCES "u" ("b")')
    assert is_already_exists_error(
        Exception('constraint "fk_x" for relation "t" already exists'), statement)


def test_without_a_statement_the_old_answer_is_unchanged():
    assert is_already_exists_error(Exception('relation "primary" already exists'))
