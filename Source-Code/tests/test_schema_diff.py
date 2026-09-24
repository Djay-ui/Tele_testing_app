"""Tests for tgdatabridge.core.schema_diff -- case-insensitive source-vs-target
name diffing, including the package-flattening and Db2 multi-event-trigger
expansion special cases."""
from tgdatabridge.core.schema_diff import compute_diff
from tgdatabridge.core.schema_model import Routine, Schema, Sequence, Table, View
from tgdatabridge.core.target_introspector import TargetObjects


def _schema(**kwargs) -> Schema:
    defaults = dict(name="HR", source_engine="Oracle", target_engine="PostgreSQL")
    defaults.update(kwargs)
    return Schema(**defaults)


def test_tables_only_in_source():
    schema = _schema(tables=[Table(name="EMPLOYEES", schema="HR"), Table(name="DEPARTMENTS", schema="HR")])
    target = TargetObjects(tables=["EMPLOYEES"])
    diff = compute_diff(schema, target)
    assert diff.tables.only_in_source == ["DEPARTMENTS"]
    assert diff.tables.only_in_target == []
    assert diff.tables.in_both == ["EMPLOYEES"]
    assert not diff.tables.in_sync


def test_tables_only_in_target():
    schema = _schema(tables=[Table(name="EMPLOYEES", schema="HR")])
    target = TargetObjects(tables=["EMPLOYEES", "LEGACY_AUDIT"])
    diff = compute_diff(schema, target)
    assert diff.tables.only_in_target == ["LEGACY_AUDIT"]
    assert diff.tables.only_in_source == []


def test_case_insensitive_matching():
    schema = _schema(tables=[Table(name="EMPLOYEES", schema="HR")])
    target = TargetObjects(tables=["employees"])  # postgres-folded lowercase
    diff = compute_diff(schema, target)
    assert diff.tables.only_in_source == []
    assert diff.tables.only_in_target == []
    assert diff.tables.in_both == ["EMPLOYEES"]  # keeps source-side casing
    assert diff.tables.in_sync


def test_views_and_sequences_diffed_independently():
    schema = _schema(
        views=[View(name="V_ACTIVE_EMP", schema="HR", definition="SELECT 1")],
        sequences=[Sequence(name="EMP_SEQ", schema="HR")],
    )
    target = TargetObjects(views=[], sequences=["EMP_SEQ"])
    diff = compute_diff(schema, target)
    assert diff.views.only_in_source == ["V_ACTIVE_EMP"]
    assert diff.sequences.in_sync


def test_plain_procedure_matches_by_name():
    schema = _schema(routines=[Routine(name="RAISE_SALARY", schema="HR", kind="PROCEDURE", source="...")])
    target = TargetObjects(routines=["RAISE_SALARY"])
    diff = compute_diff(schema, target)
    assert diff.routines.in_sync


def test_package_spec_excluded_from_expected_names():
    schema = _schema(routines=[Routine(name="PKG_HR", schema="HR", kind="PACKAGE", source="...")])
    target = TargetObjects(routines=[])
    diff = compute_diff(schema, target)
    # the spec itself never produces a target object -- it should not show
    # up as "only in source" (that would be a false positive)
    assert diff.routines.only_in_source == []
    assert diff.routines.in_sync


def test_package_body_flattened_into_member_names():
    source = (
        "PACKAGE BODY PKG_HR IS\n"
        "  PROCEDURE RAISE_SALARY(p_id NUMBER) IS BEGIN NULL; END;\n"
        "  FUNCTION GET_TOTAL RETURN NUMBER IS BEGIN RETURN 1; END;\n"
        "END PKG_HR;"
    )
    schema = _schema(routines=[Routine(name="PKG_HR", schema="HR", kind="PACKAGE BODY", source=source)])
    target = TargetObjects(routines=["PKG_HR_RAISE_SALARY", "PKG_HR_GET_TOTAL"])
    diff = compute_diff(schema, target)
    assert diff.routines.in_sync
    assert diff.routines.in_both == ["PKG_HR_GET_TOTAL", "PKG_HR_RAISE_SALARY"]


def test_package_body_flattened_member_missing_on_target():
    source = (
        "PACKAGE BODY PKG_HR IS\n"
        "  PROCEDURE RAISE_SALARY(p_id NUMBER) IS BEGIN NULL; END;\n"
        "  FUNCTION GET_TOTAL RETURN NUMBER IS BEGIN RETURN 1; END;\n"
        "END PKG_HR;"
    )
    schema = _schema(routines=[Routine(name="PKG_HR", schema="HR", kind="PACKAGE BODY", source=source)])
    target = TargetObjects(routines=["PKG_HR_RAISE_SALARY"])  # GET_TOTAL not deployed yet
    diff = compute_diff(schema, target)
    assert diff.routines.only_in_source == ["PKG_HR_GET_TOTAL"]
    assert not diff.routines.in_sync


def test_db2_multi_event_trigger_expands_to_per_event_names():
    schema = _schema(
        target_engine="DB2",
        routines=[Routine(
            name="TRG_AUDIT", schema="HR", kind="TRIGGER", source="...",
            table_name="EMPLOYEES", timing="AFTER", events=["INSERT", "UPDATE"],
        )],
    )
    target = TargetObjects(routines=["TRG_AUDIT_INSERT", "TRG_AUDIT_UPDATE"])
    diff = compute_diff(schema, target)
    assert diff.routines.in_sync


def test_non_db2_multi_event_trigger_stays_as_single_name():
    schema = _schema(
        target_engine="PostgreSQL",
        routines=[Routine(
            name="TRG_AUDIT", schema="HR", kind="TRIGGER", source="...",
            table_name="EMPLOYEES", timing="AFTER", events=["INSERT", "UPDATE"],
        )],
    )
    # The companion function a PostgreSQL trigger needs is expected too.
    target = TargetObjects(routines=["TRG_AUDIT", "TRG_AUDIT_FN"])
    diff = compute_diff(schema, target)
    assert diff.routines.in_sync


def test_fully_in_sync_across_all_categories():
    schema = _schema(
        tables=[Table(name="T1", schema="HR")],
        views=[View(name="V1", schema="HR", definition="SELECT 1")],
        sequences=[Sequence(name="S1", schema="HR")],
        routines=[Routine(name="P1", schema="HR", kind="PROCEDURE", source="...")],
    )
    target = TargetObjects(tables=["T1"], views=["V1"], sequences=["S1"], routines=["P1"])
    diff = compute_diff(schema, target)
    assert diff.fully_in_sync


def test_not_fully_in_sync_when_any_category_differs():
    schema = _schema(tables=[Table(name="T1", schema="HR")])
    target = TargetObjects(tables=[])
    diff = compute_diff(schema, target)
    assert not diff.fully_in_sync
