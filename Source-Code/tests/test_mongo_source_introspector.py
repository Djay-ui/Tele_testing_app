"""Tests for tgdatabridge.core.mongo_source_introspector -- inferring a Schema by
sampling MongoDB documents, for using MongoDB as a *source* engine. Like
tests/test_mongo_connector.py, this needs no real pymongo/bson driver:
BSON-specific types are recognized by class name only (see
mongo_source_introspector._bson_type_name's own docstring), so plain
Python values plus small same-named fake classes exercise every path a
real driver would produce."""
import datetime
import json

from tgdatabridge.core.mongo_source_introspector import _bson_type_name, introspect_schema


class _FakeObjectId:
    __name__ = "ObjectId"

    def __init__(self, hex_value):
        self._hex = hex_value

    def __str__(self):
        return self._hex


_FakeObjectId.__name__ = "ObjectId"


class _FakeDecimal128:
    def __init__(self, value):
        self._value = value

    def to_decimal(self):
        return self._value


_FakeDecimal128.__name__ = "Decimal128"


class _FakeBinary(bytes):
    pass


_FakeBinary.__name__ = "Binary"


class _FakeCollection:
    def __init__(self, docs, indexes=None):
        self._docs = docs
        self._indexes = indexes if indexes is not None else [{"key": {"_id": 1}, "name": "_id_"}]

    def aggregate(self, pipeline):
        size = pipeline[0]["$sample"]["size"]
        return list(self._docs[:size])

    def list_indexes(self):
        return list(self._indexes)


class _FakeDB:
    def __init__(self, collections, views=None):
        self._collections = collections
        self._views = views or []

    def list_collections(self, filter=None):  # noqa: A002 - mirrors pymongo's own kwarg name
        entries = [{"name": name, "type": "collection"} for name in self._collections]
        entries += self._views
        return entries

    def __getitem__(self, name):
        return self._collections[name]


class _FakeConn:
    def __init__(self, db):
        self.db = db


def _schema_for(collections, views=None):
    conn = _FakeConn(_FakeDB(collections, views))
    return introspect_schema(conn, "testdb")


# --------------------------------------------------------------- _bson_type_name


def test_bson_type_name_bool_before_int():
    assert _bson_type_name(True) == "bool"


def test_bson_type_name_int_boundary():
    assert _bson_type_name(2_147_483_647) == "int"
    assert _bson_type_name(2_147_483_648) == "long"
    assert _bson_type_name(-2_147_483_648) == "int"
    assert _bson_type_name(-2_147_483_649) == "long"


def test_bson_type_name_float_is_double():
    assert _bson_type_name(1.5) == "double"


def test_bson_type_name_str_is_string():
    assert _bson_type_name("hi") == "string"


def test_bson_type_name_bytes_is_bindata():
    assert _bson_type_name(b"raw") == "binData"


def test_bson_type_name_datetime_is_date():
    assert _bson_type_name(datetime.datetime(2024, 1, 1)) == "date"


def test_bson_type_name_objectid_by_class_name():
    assert _bson_type_name(_FakeObjectId("abc")) == "objectId"


def test_bson_type_name_decimal128_by_class_name():
    assert _bson_type_name(_FakeDecimal128("1.5")) == "decimal"


def test_bson_type_name_binary_by_class_name():
    assert _bson_type_name(_FakeBinary(b"x")) == "binData"


def test_bson_type_name_unrecognized_object_is_unknown():
    class Something:
        pass
    assert _bson_type_name(Something()) == "unknown"


# ------------------------------------------------------------------ basic table


def test_introspect_flat_collection_builds_id_and_scalar_columns():
    docs = [
        {"_id": 1, "NAME": "Alice", "AGE": 30},
        {"_id": 2, "NAME": "Bob", "AGE": 25},
    ]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    assert schema.source_engine == "MongoDB"
    assert [t.name for t in schema.tables] == ["EMPLOYEES"]
    table = schema.tables[0]
    col_names = [c.name for c in table.columns]
    assert col_names == ["_id", "AGE", "NAME"] or set(col_names) == {"_id", "AGE", "NAME"}
    id_col = next(c for c in table.columns if c.name == "_id")
    assert id_col.nullable is False
    assert any(cons.kind == "PRIMARY KEY" and cons.columns == ["_id"] for cons in table.constraints)


def test_introspect_id_column_uses_objectid_mapping():
    docs = [{"_id": _FakeObjectId("507f1f77bcf86cd799439011"), "NAME": "Alice"}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    id_col = next(c for c in schema.tables[0].columns if c.name == "_id")
    assert id_col.data_type == "ROWID"


def test_introspect_field_missing_from_some_documents_is_nullable():
    docs = [{"_id": 1, "NICKNAME": "Al"}, {"_id": 2}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    nick = next(c for c in schema.tables[0].columns if c.name == "NICKNAME")
    assert nick.nullable is True


def test_introspect_field_always_null_maps_to_varchar_with_warning():
    docs = [{"_id": 1, "NOTES": None}, {"_id": 2, "NOTES": None}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    notes = next(c for c in schema.tables[0].columns if c.name == "NOTES")
    assert notes.data_type == "VARCHAR2(4000)"
    assert any(i.severity == "warning" for i in notes.source_issues)


def test_introspect_type_conflict_uses_dominant_type_and_warns():
    docs = [{"_id": 1, "SCORE": 5}, {"_id": 2, "SCORE": 7}, {"_id": 3, "SCORE": "high"}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    table = schema.tables[0]
    score = next(c for c in table.columns if c.name == "SCORE")
    assert score.data_type == "NUMBER(9)"  # dominant type was "int" (2 of 3)
    assert any("SCORE" in i.message for i in table.issues) or any(
        "SCORE" in i.message for i in score.source_issues
    )


def test_introspect_empty_sample_creates_id_only_table_with_warning():
    schema = _schema_for({"EMPTY_COLL": _FakeCollection([])})
    table = schema.tables[0]
    assert [c.name for c in table.columns] == ["_id"]
    assert any(i.severity == "warning" for i in table.issues)


def test_introspect_excludes_counters_helper_collection():
    schema = _schema_for({
        "EMPLOYEES": _FakeCollection([{"_id": 1}]),
        "counters": _FakeCollection([{"_id": "EMP_SEQ", "seq": 1}]),
    })
    assert "counters" not in [t.name for t in schema.tables]


# ----------------------------------------------------------- nested sub-objects


def test_introspect_nested_object_flattens_into_dotted_columns():
    docs = [
        {"_id": 1, "ADDRESS": {"CITY": "Springfield", "GEO": {"LAT": 1.5}}},
        {"_id": 2, "ADDRESS": {"CITY": "Shelbyville", "GEO": {"LAT": 2.5}}},
    ]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    col_names = {c.name for c in schema.tables[0].columns}
    assert "ADDRESS.CITY" in col_names
    assert "ADDRESS.GEO.LAT" in col_names


# ------------------------------------------------------------------ array fields


def test_introspect_array_of_scalars_becomes_child_table():
    docs = [{"_id": 1, "TAGS": ["a", "b"]}, {"_id": 2, "TAGS": ["c"]}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    parent = next(t for t in schema.tables if t.name == "EMPLOYEES")
    assert "TAGS" not in [c.name for c in parent.columns]
    child = next(t for t in schema.tables if t.name == "EMPLOYEES_TAGS")
    col_names = [c.name for c in child.columns]
    assert col_names == ["EMPLOYEES_TAGS_ID", "EMPLOYEES_ID", "value"]
    assert child.source_collection == "EMPLOYEES"
    assert child.source_array_path == "TAGS"
    pk = next(cons for cons in child.constraints if cons.kind == "PRIMARY KEY")
    assert pk.columns == ["EMPLOYEES_TAGS_ID"]
    fk = next(cons for cons in child.constraints if cons.kind == "FOREIGN KEY")
    assert fk.columns == ["EMPLOYEES_ID"]
    assert fk.ref_table == "EMPLOYEES"
    assert fk.ref_columns == ["_id"]


def test_introspect_array_of_subdocuments_becomes_child_table_with_item_columns():
    docs = [{"_id": 1, "ITEMS": [{"SKU": "X1", "QTY": 2}, {"SKU": "X2", "QTY": 1}]}]
    schema = _schema_for({"ORDERS": _FakeCollection(docs)})
    child = next(t for t in schema.tables if t.name == "ORDERS_ITEMS")
    col_names = [c.name for c in child.columns]
    assert col_names == ["ORDERS_ITEMS_ID", "ORDERS_ID", "QTY", "SKU"]


def test_introspect_array_always_empty_creates_child_table_with_info_issue():
    docs = [{"_id": 1, "TAGS": []}, {"_id": 2, "TAGS": []}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    child = next(t for t in schema.tables if t.name == "EMPLOYEES_TAGS")
    assert [c.name for c in child.columns] == ["EMPLOYEES_TAGS_ID", "EMPLOYEES_ID"]
    assert any(i.severity == "info" for i in child.issues)


def test_introspect_array_of_arrays_flagged_and_not_expanded():
    docs = [{"_id": 1, "MATRIX": [[1, 2], [3, 4]]}]
    schema = _schema_for({"EMPLOYEES": _FakeCollection(docs)})
    child = next(t for t in schema.tables if t.name == "EMPLOYEES_MATRIX")
    assert [c.name for c in child.columns] == ["EMPLOYEES_MATRIX_ID", "EMPLOYEES_ID"]
    assert any("array-of-arrays" in i.message for i in child.issues)


def test_introspect_array_nested_inside_array_item_is_dropped_with_warning():
    docs = [{"_id": 1, "ITEMS": [{"SKU": "X1", "SUBTAGS": ["a", "b"]}]}]
    schema = _schema_for({"ORDERS": _FakeCollection(docs)})
    child = next(t for t in schema.tables if t.name == "ORDERS_ITEMS")
    assert "SUBTAGS" not in [c.name for c in child.columns]
    assert any("nested inside another array" in i.message for i in child.issues)


# ---------------------------------------------------------------------- indexes


def test_introspect_indexes_skips_default_id_index():
    docs = [{"_id": 1, "NAME": "Alice"}]
    coll = _FakeCollection(docs, indexes=[{"key": {"_id": 1}, "name": "_id_"}])
    schema = _schema_for({"EMPLOYEES": coll})
    assert schema.tables[0].indexes == []


def test_introspect_indexes_keeps_normal_field_index():
    docs = [{"_id": 1, "NAME": "Alice"}]
    coll = _FakeCollection(docs, indexes=[
        {"key": {"_id": 1}, "name": "_id_"},
        {"key": {"NAME": 1}, "name": "NAME_1", "unique": True},
    ])
    schema = _schema_for({"EMPLOYEES": coll})
    idx = schema.tables[0].indexes[0]
    assert idx.name == "NAME_1"
    assert idx.columns == ["NAME"]
    assert idx.unique is True


def test_introspect_indexes_skips_index_on_array_promoted_field_with_warning():
    docs = [{"_id": 1, "NAME": "Alice", "TAGS": ["a"]}]
    coll = _FakeCollection(docs, indexes=[
        {"key": {"_id": 1}, "name": "_id_"},
        {"key": {"TAGS": 1}, "name": "TAGS_1"},
    ])
    schema = _schema_for({"EMPLOYEES": coll})
    parent = schema.tables[0]
    assert parent.indexes == []
    assert any("child table" in i.message for i in parent.issues)


# ------------------------------------------------------------------------ views


def test_introspect_views_are_reported_with_mongodb_source_engine():
    views = [{
        "name": "V_ACTIVE",
        "type": "view",
        "options": {"viewOn": "EMPLOYEES", "pipeline": [{"$match": {"ACTIVE": True}}]},
    }]
    schema = _schema_for({"EMPLOYEES": _FakeCollection([{"_id": 1}])}, views=views)
    assert len(schema.views) == 1
    view = schema.views[0]
    assert view.name == "V_ACTIVE"
    assert view.source_engine == "MongoDB"
    parsed = json.loads(view.definition)
    assert parsed["viewOn"] == "EMPLOYEES"
    assert parsed["pipeline"] == [{"$match": {"ACTIVE": True}}]


# -------------------------------------------------------- sequences/routines


def test_introspect_sequences_and_routines_always_empty():
    schema = _schema_for({"EMPLOYEES": _FakeCollection([{"_id": 1}])})
    assert schema.sequences == []
    assert schema.routines == []
