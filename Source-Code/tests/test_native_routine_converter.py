"""Routines and triggers from a MySQL or PostgreSQL source.

The live suite (repack/trigger_live.py) proves the output applies to real
servers and that the migrated triggers fire. These pin the pieces that
would otherwise regress quietly -- above all the handful of rewrites that
produce SQL which still *executes* while doing the wrong thing.
"""
from __future__ import annotations

import pytest

from tgdatabridge.core import native_routine_converter as N
from tgdatabridge.core.plsql_converter import convert_routine as dispatch
from tgdatabridge.core.schema_model import ConversionStatus, Routine, RoutineParameter


def routine(source, engine, kind="PROCEDURE", name="p", **kw):
    return Routine(name=name, schema="app", kind=kind, source=source,
                   source_engine=engine, **kw)


def convert(source, engine, target, **kw):
    return N.convert_routine(routine(source, engine, **kw), target)


def sql(source, engine, target, **kw):
    return convert(source, engine, target, **kw).converted_source


TRIGGER = dict(kind="TRIGGER", name="trg_x", table_name="users",
               timing="BEFORE", events=["INSERT"], row_level=True)


# ------------------------------------------------------------- dispatch

@pytest.mark.parametrize("source_engine,target", [
    ("MySQL", "MySQL"), ("MySQL", "MariaDB"), ("MySQL", "PostgreSQL"),
    ("PostgreSQL", "PostgreSQL"), ("PostgreSQL", "MySQL"),
])
def test_the_pairs_that_now_convert(source_engine, target):
    """Every one of these used to be flagged MANUAL on sight -- including
    MySQL -> MySQL, where there was nothing to translate at all."""
    body = ("BEGIN\n  SET NEW.tag = 'x';\nEND" if source_engine == "MySQL"
            else "BEGIN\n  NEW.tag := 'x';\n  RETURN NEW;\nEND")
    result = dispatch(routine(body, source_engine, **TRIGGER), target)
    assert result.status != ConversionStatus.MANUAL
    assert "CREATE TRIGGER" in result.converted_source


def test_a_pair_with_no_front_end_is_still_refused():
    result = convert("BEGIN\n  NULL;\nEND", "MySQL", "Oracle")
    assert result.status == ConversionStatus.MANUAL
    assert "not automated" in result.issues[0].message


def test_mariadb_counts_as_mysql():
    assert N.same_engine("MariaDB", "MySQL")
    assert N.same_engine("MySQL", "MariaDB")
    assert not N.same_engine("MySQL", "PostgreSQL")


# ------------------------------------------------- same-engine rebuilding

def test_a_mysql_trigger_is_rebuilt_with_the_header_its_catalog_never_stored():
    """information_schema.triggers.action_statement is the body alone, so
    there was nothing to apply without reassembling the CREATE."""
    out = sql("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "MySQL", **TRIGGER)
    assert "CREATE TRIGGER `trg_x`" in out
    assert "BEFORE INSERT ON `users`" in out
    assert "FOR EACH ROW" in out
    assert "SET NEW.tag = 'x';" in out


def test_a_rebuilt_mysql_routine_is_droppable_so_the_script_reruns():
    """MySQL has no CREATE OR REPLACE PROCEDURE."""
    out = sql("BEGIN\n  NULL;\nEND", "MySQL", "MySQL")
    assert "DROP PROCEDURE IF EXISTS `p`;" in out
    assert out.index("DROP PROCEDURE") < out.index("CREATE PROCEDURE")


def test_a_rebuilt_mysql_procedure_gets_its_parameters_back():
    result = convert("BEGIN\n  NULL;\nEND", "MySQL", "MySQL", parameters=[
        RoutineParameter("p_id", "int", "IN"),
        RoutineParameter("p_out", "varchar(40)", "OUT"),
    ])
    assert "CREATE PROCEDURE `p`(IN `p_id` int, OUT `p_out` varchar(40))" \
        in result.converted_source


def test_a_rebuilt_mysql_function_gets_its_return_type_and_a_characteristic():
    out = sql("BEGIN\n  RETURN 1;\nEND", "MySQL", "MySQL",
              kind="FUNCTION", return_type="int")
    assert "CREATE FUNCTION `p`()" in out
    assert "RETURNS int" in out
    assert any(word in out for word in
               ("DETERMINISTIC", "READS SQL DATA", "MODIFIES SQL DATA"))


def test_a_rebuilt_postgres_trigger_recreates_its_companion_function():
    out = sql("BEGIN\n  RETURN NEW;\nEND", "PostgreSQL", "PostgreSQL", **TRIGGER)
    assert 'CREATE OR REPLACE FUNCTION "trg_x_fn"()' in out
    assert "RETURNS TRIGGER" in out
    assert 'CREATE TRIGGER "trg_x"' in out
    assert 'EXECUTE FUNCTION "trg_x_fn"();' in out


def test_a_rebuilt_postgres_trigger_drops_itself_first():
    out = sql("BEGIN\n  RETURN NEW;\nEND", "PostgreSQL", "PostgreSQL", **TRIGGER)
    assert 'DROP TRIGGER IF EXISTS "trg_x" ON "users";' in out


def test_an_empty_body_is_reported_rather_than_emitted_as_a_broken_create():
    """A MySQL account without rights on the routine's definer gets an
    empty routine_definition -- silently emitting `CREATE PROCEDURE p()`
    with no body would be worse than saying so."""
    result = convert("", "MySQL", "MySQL")
    assert result.status == ConversionStatus.MANUAL
    assert any("empty body" in i.message for i in result.issues)


def test_sqlserver_and_db2_pass_their_full_create_through():
    out = sql("CREATE PROCEDURE p AS SELECT 1", "SQL Server", "SQL Server")
    assert out.startswith("CREATE OR ALTER PROCEDURE")
    out = sql("CREATE PROCEDURE p BEGIN END", "Db2", "Db2")
    assert out.startswith("CREATE PROCEDURE")


# ------------------------------------------------- MySQL -> PostgreSQL

def test_a_mysql_trigger_becomes_a_function_plus_a_trigger():
    out = sql("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "PostgreSQL", **TRIGGER)
    assert 'CREATE OR REPLACE FUNCTION "trg_x_fn"()' in out
    assert "RETURNS TRIGGER" in out
    assert "NEW.tag := 'x';" in out
    assert 'EXECUTE FUNCTION "trg_x_fn"();' in out


def test_a_return_is_added_because_a_before_trigger_without_one_drops_the_row():
    result = convert("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "PostgreSQL", **TRIGGER)
    assert "RETURN NEW;" in result.converted_source
    assert any("discards the row" in i.message for i in result.issues)


def test_an_after_trigger_returns_null():
    out = sql("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "PostgreSQL",
              **{**TRIGGER, "timing": "AFTER"})
    assert "RETURN NULL;" in out


def test_set_becomes_assignment_but_an_update_set_is_left_alone():
    """`UPDATE t SET c = v` has a SET too. Rewriting that one produced
    `UPDATE t c := v`, which PostgreSQL rejects at the `:=`."""
    out = sql("BEGIN\n  UPDATE people SET tag = 'x' WHERE id = 1;\n"
              "  SET v = 2;\nEND", "MySQL", "PostgreSQL")
    assert "UPDATE people SET tag = 'x' WHERE id = 1;" in out
    assert "v := 2;" in out


def test_elseif_becomes_elsif():
    out = sql("BEGIN\n  IF a THEN SET v = 1; ELSEIF b THEN SET v = 2; END IF;\nEND",
              "MySQL", "PostgreSQL")
    assert "ELSIF" in out and "ELSEIF" not in out


def test_while_do_becomes_while_loop():
    out = sql("BEGIN\n  WHILE v < 3 DO\n    SET v = v + 1;\n  END WHILE;\nEND",
              "MySQL", "PostgreSQL")
    assert "WHILE v < 3 LOOP" in out
    assert "END LOOP;" in out
    assert "END WHILE" not in out


def test_repeat_until_becomes_a_loop_with_an_exit():
    out = sql("BEGIN\n  REPEAT\n    SET v = v + 1;\n  UNTIL v > 3\n  END REPEAT;\nEND",
              "MySQL", "PostgreSQL")
    assert "LOOP" in out and "EXIT WHEN v > 3;" in out


def test_leave_and_iterate_become_exit_and_continue():
    out = sql("BEGIN\n  lbl: LOOP\n    LEAVE lbl;\n    ITERATE lbl;\n  END LOOP lbl;\nEND",
              "MySQL", "PostgreSQL")
    assert "<<lbl>> LOOP" in out
    assert "EXIT lbl;" in out and "CONTINUE lbl;" in out
    assert "END LOOP;" in out


def test_signal_becomes_raise_exception():
    out = sql("BEGIN\n  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'boom';\nEND",
              "MySQL", "PostgreSQL")
    assert "RAISE EXCEPTION 'boom'" in out


def test_declarations_move_into_a_declare_section():
    """MySQL declares inside BEGIN; PL/pgSQL wants them before it."""
    out = sql("BEGIN\n  DECLARE v INT DEFAULT 0;\n  DECLARE w VARCHAR(10);\n"
              "  SET v = 1;\nEND", "MySQL", "PostgreSQL")
    assert out.index("DECLARE") < out.index("BEGIN")
    assert "v INT := 0;" in out
    assert "w VARCHAR(10);" in out


def test_backticks_become_double_quotes():
    out = sql("BEGIN\n  SET NEW.`userId` = 1;\nEND", "MySQL", "PostgreSQL", **TRIGGER)
    assert '"userId"' in out
    assert "`" not in out


def test_mysql_builtins_are_translated():
    out = sql("BEGIN\n  SET v = IFNULL(a, b);\n  SET w = NOW();\n"
              "  SET x = IF(a > 1, 'y', 'n');\nEND", "MySQL", "PostgreSQL")
    assert "COALESCE(a, b)" in out
    assert "now()" in out
    assert "CASE WHEN a > 1 THEN 'y' ELSE 'n' END" in out


def test_a_date_format_mask_is_translated_not_copied():
    out = sql("BEGIN\n  SET v = DATE_FORMAT(d, '%d/%m/%Y');\nEND", "MySQL", "PostgreSQL")
    assert "to_char(d, 'DD/MM/YYYY')" in out


@pytest.mark.parametrize("fragment,phrase", [
    ("DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;", "HANDLER"),
    ("GET DIAGNOSTICS v = ROW_COUNT;", "GET DIAGNOSTICS"),
    ("SET v = GROUP_CONCAT(name);", "GROUP_CONCAT"),
])
def test_mysql_constructs_with_no_postgres_equivalent_are_flagged(fragment, phrase):
    result = convert(f"BEGIN\n  {fragment}\nEND", "MySQL", "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL
    assert any(phrase in i.message for i in result.issues)


# ------------------------------------------------- PostgreSQL -> MySQL

def test_a_postgres_trigger_is_inlined_back_into_the_trigger_body():
    out = sql("BEGIN\n  NEW.tag := 'x';\n  RETURN NEW;\nEND",
              "PostgreSQL", "MySQL", **TRIGGER)
    assert "CREATE TRIGGER `trg_x`" in out
    assert "SET NEW.tag = 'x';" in out
    assert "EXECUTE FUNCTION" not in out


def test_return_new_is_removed_because_mysql_rejects_it_in_a_trigger():
    out = sql("BEGIN\n  NEW.tag := 'x';\n  RETURN NEW;\nEND",
              "PostgreSQL", "MySQL", **TRIGGER)
    assert "RETURN NEW" not in out


def test_concatenation_becomes_concat_for_a_mysql_target():
    """`||` is logical OR in MySQL, so leaving it turns every built string
    into 0 or 1 rather than failing loudly."""
    out = sql("BEGIN\n  NEW.note := 'created ' || NEW.name;\n  RETURN NEW;\nEND",
              "PostgreSQL", "MySQL", **TRIGGER)
    assert "CONCAT('created ', NEW.name)" in out
    assert "||" not in out


def test_elsif_becomes_elseif_going_the_other_way():
    out = sql("BEGIN\n  IF a THEN v := 1; ELSIF b THEN v := 2; END IF;\nEND",
              "PostgreSQL", "MySQL")
    assert "ELSEIF" in out


def test_raise_exception_becomes_signal():
    out = sql("BEGIN\n  RAISE EXCEPTION 'boom';\nEND", "PostgreSQL", "MySQL")
    assert "SIGNAL SQLSTATE '45000'" in out
    assert "MESSAGE_TEXT = 'boom'" in out


def test_the_declare_section_folds_back_inside_begin():
    out = sql("DECLARE\n  v INT := 0;\nBEGIN\n  v := 1;\nEND", "PostgreSQL", "MySQL")
    assert "DECLARE v INT DEFAULT 0;" in out
    assert out.index("BEGIN") < out.index("DECLARE v INT")
    assert "SET v = 1;" in out


def test_a_statement_level_trigger_is_flagged_because_mysql_has_none():
    result = convert("BEGIN\n  RETURN NULL;\nEND", "PostgreSQL", "MySQL",
                     **{**TRIGGER, "row_level": False})
    assert result.status == ConversionStatus.MANUAL
    assert any("FOR EACH ROW" in i.message for i in result.issues)


def test_an_instead_of_trigger_is_flagged():
    result = convert("BEGIN\n  RETURN NULL;\nEND", "PostgreSQL", "MySQL",
                     **{**TRIGGER, "timing": "INSTEAD OF"})
    assert any("INSTEAD OF" in i.message for i in result.issues)


@pytest.mark.parametrize("fragment,phrase", [
    ("PERFORM do_thing();", "PERFORM"),
    ("v := x::integer;", "::"),
    ("RETURN QUERY SELECT 1;", "set-returning"),
])
def test_postgres_constructs_with_no_mysql_equivalent_are_flagged(fragment, phrase):
    result = convert(f"BEGIN\n  {fragment}\nEND", "PostgreSQL", "MySQL")
    assert result.status == ConversionStatus.MANUAL
    assert any(phrase in i.message for i in result.issues)


# ------------------------------------------------------------- triggers

def test_a_multi_event_trigger_is_split_for_a_mysql_target():
    result = convert("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "MySQL",
                     **{**TRIGGER, "events": ["INSERT", "UPDATE"]})
    assert "CREATE TRIGGER `trg_x_INSERT`" in result.converted_source
    assert "CREATE TRIGGER `trg_x_UPDATE`" in result.converted_source
    assert any("one event per trigger" in i.message for i in result.issues)


def test_a_multi_event_trigger_stays_one_object_for_a_postgres_target():
    out = sql("BEGIN\n  SET NEW.tag = 'x';\nEND", "MySQL", "PostgreSQL",
              **{**TRIGGER, "events": ["INSERT", "UPDATE"]})
    assert "BEFORE INSERT OR UPDATE ON" in out
    assert out.count("CREATE TRIGGER") == 1


def test_a_trigger_with_no_table_says_so_rather_than_emitting_a_silent_stub():
    result = convert("BEGIN\n  NULL;\nEND", "MySQL", "MySQL",
                     **{**TRIGGER, "table_name": None})
    assert result.status == ConversionStatus.MANUAL
    assert "UNKNOWN_TABLE" in result.converted_source


# -------------------------------------------------------------- masking

def test_a_hash_inside_a_string_is_not_read_as_a_comment():
    """MySQL's `#` starts a comment, so masking comments before strings
    ate the `#` in `CONCAT(v, ' #', id)` and swallowed the rest of the
    line -- turning a good function into an unterminated one."""
    out = sql("BEGIN\n  SET v = CONCAT(name, ' #', id);\nEND", "MySQL", "PostgreSQL")
    assert "' #'" in out
    assert "id)" in out


def test_a_keyword_inside_a_string_is_not_rewritten():
    out = sql("BEGIN\n  SET v = 'END WHILE is not code';\nEND", "MySQL", "PostgreSQL")
    assert "'END WHILE is not code'" in out


def test_an_apostrophe_in_a_comment_does_not_swallow_the_code_after_it():
    out = sql("BEGIN\n  -- don't break\n  SET v = 1;\nEND", "MySQL", "PostgreSQL")
    assert "v := 1;" in out


def test_conversion_never_raises_on_junk():
    for junk in ("", "   ", "BEGIN", "END", "))))", "SET"):
        for engine in ("MySQL", "PostgreSQL"):
            for target in ("MySQL", "PostgreSQL"):
                convert(junk, engine, target)
