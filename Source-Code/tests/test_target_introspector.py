"""Tests for tgdatabridge.core.target_introspector -- the lightweight catalog
queries that populate the compact "Target Schema (as migrated)" pane."""
from tgdatabridge.core import target_introspector as ti


class _FakeConnector:
    """Records every (sql, params) call and returns canned rows keyed by a
    substring of the SQL (e.g. 'information_schema.tables')."""

    def __init__(self, canned: dict):
        self.canned = canned
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        for marker, rows in self.canned.items():
            if marker in sql:
                return rows
        return []


def test_introspect_target_postgres_lists_all_categories():
    connector = _FakeConnector({
        "information_schema.tables": [("account",), ("customer",)],
        "information_schema.views": [("vw_customer_account",)],
        "information_schema.sequences": [("seq_account",)],
        "information_schema.routines": [("pkg_bank_get_customer",)],
        "information_schema.triggers": [("trg_emp_audit",), ("trg_emp_audit",)],  # dup event rows
    })
    result = ti.introspect_target_postgres(connector, "public")
    assert result.tables == ["account", "customer"]
    assert result.views == ["vw_customer_account"]
    assert result.sequences == ["seq_account"]
    assert result.routines == ["pkg_bank_get_customer", "trg_emp_audit"]  # de-duplicated, sorted


def test_introspect_target_postgres_passes_schema_param():
    connector = _FakeConnector({})
    ti.introspect_target_postgres(connector, "hr")
    assert all(params == {"schema": "hr"} for _sql, params in connector.calls)


def test_introspect_target_mysql_has_no_native_sequences():
    connector = _FakeConnector({
        "information_schema.tables": [("account",), ("emp_seq_seq",)],
        "information_schema.views": [],
        "information_schema.routines": [("get_total",)],
        "information_schema.triggers": [],
    })
    result = ti.introspect_target_mysql(connector, "hrdb")
    assert result.sequences == []  # no native SEQUENCE object in MySQL
    assert "emp_seq_seq" in result.tables  # the emulated helper table shows up as a table instead


def test_introspect_target_dispatches_on_engine_name():
    pg_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    result = ti.introspect_target(pg_connector, "PostgreSQL", "public")
    assert result.tables == ["t"]

    my_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    result = ti.introspect_target(my_connector, "MySQL", "db")
    assert result.tables == ["t"]
    assert result.sequences == []


def test_introspect_target_oracle_lists_all_categories():
    connector = _FakeConnector({
        "all_tables": [("ACCOUNT",), ("CUSTOMER",)],
        "all_views": [("VW_CUSTOMER_ACCOUNT",)],
        "all_sequences": [("SEQ_ACCOUNT",)],
        "all_objects": [("PKG_BANK",), ("TRG_EMP_AUDIT",)],
    })
    result = ti.introspect_target_oracle(connector, "HR")
    assert result.tables == ["ACCOUNT", "CUSTOMER"]
    assert result.views == ["VW_CUSTOMER_ACCOUNT"]
    assert result.sequences == ["SEQ_ACCOUNT"]
    assert result.routines == ["PKG_BANK", "TRG_EMP_AUDIT"]


def test_introspect_target_oracle_passes_owner_bind():
    connector = _FakeConnector({})
    ti.introspect_target_oracle(connector, "HR")
    assert all(params == {"owner": "HR"} for _sql, params in connector.calls)


def test_introspect_target_dispatches_oracle_by_engine_name():
    connector = _FakeConnector({"all_tables": [("T",)]})
    result = ti.introspect_target(connector, "Oracle", "HR")
    assert result.tables == ["T"]


def test_introspect_target_sqlserver_lists_all_categories():
    connector = _FakeConnector({
        "information_schema.tables": [("ACCOUNT",), ("CUSTOMER",)],
        "information_schema.views": [("VW_CUSTOMER_ACCOUNT",)],
        "sys.sequences": [("SEQ_ACCOUNT",)],
        "information_schema.routines": [("PKG_BANK_GET_CUSTOMER",)],
        "sys.objects": [("TRG_EMP_AUDIT",)],
    })
    result = ti.introspect_target_sqlserver(connector, "dbo")
    assert result.tables == ["ACCOUNT", "CUSTOMER"]
    assert result.views == ["VW_CUSTOMER_ACCOUNT"]
    assert result.sequences == ["SEQ_ACCOUNT"]
    assert result.routines == ["PKG_BANK_GET_CUSTOMER", "TRG_EMP_AUDIT"]


def test_introspect_target_sqlserver_passes_schema_param():
    connector = _FakeConnector({})
    ti.introspect_target_sqlserver(connector, "dbo")
    assert all(params == {"schema": "dbo"} for _sql, params in connector.calls)


def test_introspect_target_dispatches_sqlserver_by_engine_name():
    connector = _FakeConnector({"information_schema.tables": [("T",)]})
    result = ti.introspect_target(connector, "SQL Server", "dbo")
    assert result.tables == ["T"]


def test_introspect_target_postgres_and_mysql_dispatch_unaffected_by_sqlserver_addition():
    pg_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(pg_connector, "PostgreSQL", "public").tables == ["t"]
    my_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(my_connector, "MySQL", "db").tables == ["t"]


def test_introspect_target_db2_lists_all_categories():
    # Both the tables and views queries hit SYSCAT.TABLES (differing only by
    # TYPE = 'T' vs 'V'), so the fake connector's canned dict keys on that
    # more specific substring to tell them apart.
    connector = _FakeConnector({
        "TYPE = 'T'": [("ACCOUNT",), ("CUSTOMER",)],
        "TYPE = 'V'": [("VW_CUSTOMER_ACCOUNT",)],
        "SYSCAT.SEQUENCES": [("SEQ_ACCOUNT",)],
        "SYSCAT.ROUTINES": [("PKG_BANK_GET_CUSTOMER",)],
        "SYSCAT.TRIGGERS": [("TRG_EMP_AUDIT",)],
    })
    result = ti.introspect_target_db2(connector, "APP")
    assert result.tables == ["ACCOUNT", "CUSTOMER"]
    assert result.views == ["VW_CUSTOMER_ACCOUNT"]
    assert result.sequences == ["SEQ_ACCOUNT"]
    assert result.routines == ["PKG_BANK_GET_CUSTOMER", "TRG_EMP_AUDIT"]


def test_introspect_target_db2_passes_schema_param():
    connector = _FakeConnector({})
    ti.introspect_target_db2(connector, "APP")
    assert all(params == {"schema": "APP"} for _sql, params in connector.calls)


def test_introspect_target_dispatches_db2_by_engine_name():
    connector = _FakeConnector({"TYPE = 'T'": [("T",)]})
    result = ti.introspect_target(connector, "DB2", "APP")
    assert result.tables == ["T"]


def test_introspect_target_other_engines_dispatch_unaffected_by_db2_addition():
    pg_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(pg_connector, "PostgreSQL", "public").tables == ["t"]
    my_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(my_connector, "MySQL", "db").tables == ["t"]
    sql_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(sql_connector, "SQL Server", "dbo").tables == ["t"]


# ------------------------------------------------------------------- MongoDB
#
# MongoDB has no information_schema/SYSCAT catalog at all -- introspection
# reads collections/indexes directly off a PyMongo-shaped `db` object
# (list_collection_names() / db[name].find(...)), not through
# connector.execute() the way every engine above does. This needs a
# differently-shaped fake connector from _FakeConnector above.


class _FakeMongoCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, filter_=None, projection=None):
        return list(self._docs)


class _FakeMongoDB:
    def __init__(self, collection_names, counters_docs=None):
        self._collection_names = collection_names
        self._counters_docs = counters_docs or []

    def list_collection_names(self):
        return list(self._collection_names)

    def __getitem__(self, name):
        if name == "counters":
            return _FakeMongoCollection(self._counters_docs)
        return _FakeMongoCollection([])


class _FakeMongoConnector:
    def __init__(self, collection_names, counters_docs=None):
        self.db = _FakeMongoDB(collection_names, counters_docs)


def test_introspect_target_mongodb_lists_collections_as_tables():
    connector = _FakeMongoConnector(collection_names=["EMPLOYEES", "DEPARTMENTS"])
    result = ti.introspect_target_mongodb(connector, "mydb")
    assert result.tables == ["DEPARTMENTS", "EMPLOYEES"]  # sorted


def test_introspect_target_mongodb_excludes_counters_helper_collection_from_tables():
    connector = _FakeMongoConnector(
        collection_names=["EMPLOYEES", "counters"],
        counters_docs=[{"_id": "EMP_SEQ", "seq": 1}],
    )
    result = ti.introspect_target_mongodb(connector, "mydb")
    assert "counters" not in result.tables
    assert result.tables == ["EMPLOYEES"]


def test_introspect_target_mongodb_reports_counters_documents_as_sequences():
    connector = _FakeMongoConnector(
        collection_names=["EMPLOYEES", "counters"],
        counters_docs=[{"_id": "EMP_SEQ"}, {"_id": "DEPT_SEQ"}],
    )
    result = ti.introspect_target_mongodb(connector, "mydb")
    assert result.sequences == ["DEPT_SEQ", "EMP_SEQ"]  # sorted


def test_introspect_target_mongodb_no_sequences_when_no_counters_collection():
    connector = _FakeMongoConnector(collection_names=["EMPLOYEES"])
    result = ti.introspect_target_mongodb(connector, "mydb")
    assert result.sequences == []


def test_introspect_target_mongodb_views_and_routines_always_empty():
    # generate_view_ddl's MongoDB branch and convert_routine's MongoDB
    # branch both only ever emit MANUAL-CONVERSION-REQUIRED placeholder
    # comments -- nothing real is ever created for either category, so
    # there is nothing here for either category to ever find.
    connector = _FakeMongoConnector(collection_names=["EMPLOYEES"])
    result = ti.introspect_target_mongodb(connector, "mydb")
    assert result.views == []
    assert result.routines == []


def test_introspect_target_dispatches_mongodb_by_engine_name():
    connector = _FakeMongoConnector(collection_names=["T"])
    result = ti.introspect_target(connector, "MongoDB", "mydb")
    assert result.tables == ["T"]


def test_introspect_target_other_engines_dispatch_unaffected_by_mongodb_addition():
    pg_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(pg_connector, "PostgreSQL", "public").tables == ["t"]
    db2_connector = _FakeConnector({"TYPE = 'T'": [("t",)]})
    assert ti.introspect_target(db2_connector, "DB2", "APP").tables == ["t"]


def test_introspect_target_other_engines_dispatch_unaffected_by_oracle_addition():
    pg_connector = _FakeConnector({"information_schema.tables": [("t",)]})
    assert ti.introspect_target(pg_connector, "PostgreSQL", "public").tables == ["t"]
    mongo_connector = _FakeMongoConnector(collection_names=["T"])
    assert ti.introspect_target(mongo_connector, "MongoDB", "mydb").tables == ["T"]
