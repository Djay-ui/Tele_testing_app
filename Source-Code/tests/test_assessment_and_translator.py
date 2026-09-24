from tgdatabridge.core.assessment import build_assessment
from tgdatabridge.core.schema_model import Column, ConversionStatus, Routine, Schema, Table
from tgdatabridge.core.sql_translator import score_complexity, translate_routine


def test_score_complexity_simple_routine():
    source = "BEGIN SELECT 1 INTO x FROM dual; END;"
    score, issues = score_complexity(source)
    assert score == 0


def test_score_complexity_flags_dbms_package():
    source = "BEGIN DBMS_OUTPUT.PUT_LINE('hi'); END;"
    score, issues = score_complexity(source)
    assert score >= 3
    assert any("DBMS_" in i.message for i in issues)


def test_translate_routine_marks_manual_for_complex():
    routine = Routine(name="P1", schema="HR", kind="PROCEDURE",
                       source="BEGIN DBMS_LOCK.SLEEP(1); FORALL i IN 1..10 INSERT INTO t VALUES (i); END;")
    result = translate_routine(routine, "PostgreSQL")
    assert result.status == ConversionStatus.MANUAL
    assert result.complexity_score > 0


def test_translate_routine_simple_still_flagged_for_review():
    routine = Routine(name="P2", schema="HR", kind="FUNCTION", source="BEGIN RETURN 1; END;")
    result = translate_routine(routine, "PostgreSQL")
    assert result.complexity_score == 0
    assert result.status == ConversionStatus.AUTOMATIC_WITH_WARNINGS


def test_build_assessment_counts_objects():
    schema = Schema(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    t1 = Table(name="T1", schema="HR", columns=[Column(name="A", data_type="NUMBER(3)")])
    t1.status = ConversionStatus.AUTOMATIC
    schema.tables.append(t1)

    summary = build_assessment(schema)
    assert summary.total_objects == 1
    assert summary.counts_by_type["Tables"] == 1
    assert summary.automatic_pct == 100.0
    assert summary.action_items == []
