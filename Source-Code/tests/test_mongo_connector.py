"""Tests for tgdatabridge.db.mongo_connector.MongoConnector's pure-Python logic
(argument splitting, comment detection, DDL-statement dispatch) that
doesn't require a real pymongo driver or MongoDB instance -- pymongo is
only imported lazily inside connect()/execute_ddl(), so a lightweight fake
module is installed in sys.modules before either of those run, mirroring
the fake-connector approach every other tests/test_*_connector.py file
uses for its own driver."""
import sys
import types


def _install_fake_pymongo():
    """Install a minimal fake `pymongo` package in sys.modules so
    MongoConnector's lazy `import pymongo` (inside execute_ddl(), which
    only ever needs `pymongo.errors.CollectionInvalid`) succeeds without
    the real driver installed. Idempotent -- safe to call from every test."""
    if "pymongo" in sys.modules and hasattr(sys.modules["pymongo"], "_is_fake"):
        return sys.modules["pymongo"].errors.CollectionInvalid

    pymongo_mod = types.ModuleType("pymongo")
    errors_mod = types.ModuleType("pymongo.errors")

    class CollectionInvalid(Exception):
        pass

    errors_mod.CollectionInvalid = CollectionInvalid
    pymongo_mod.errors = errors_mod
    pymongo_mod._is_fake = True

    class _FakeMongoClient:
        def __init__(self, *a, **k):
            pass

    pymongo_mod.MongoClient = _FakeMongoClient
    sys.modules["pymongo"] = pymongo_mod
    sys.modules["pymongo.errors"] = errors_mod
    return CollectionInvalid


CollectionInvalid = _install_fake_pymongo()

from tgdatabridge.db.base import ConnectionParams  # noqa: E402
from tgdatabridge.db.mongo_connector import (  # noqa: E402
    MongoConnector, _is_pure_comment_block, _split_top_level_args, _strip_leading_comment_lines,
)
from tgdatabridge.db.tls_config import TlsConfig  # noqa: E402


def _params(**overrides):
    base = dict(host="localhost", port=27017, database="testdb", username="", password="", schema=None)
    base.update(overrides)
    return ConnectionParams(**base)


class _FakeCollection:
    def __init__(self, name, store, documents=None):
        self.name = name
        self.store = store
        self._documents = documents if documents is not None else store.get("docs", {}).get(name, [])

    def create_index(self, keys, **opts):
        self.store.setdefault("indexes", []).append((self.name, keys, opts))

    def update_one(self, filter_, update, **opts):
        self.store.setdefault("updates", []).append((self.name, filter_, update, opts))

    def insert_many(self, docs):
        self.store.setdefault("inserts", []).append((self.name, docs))

    def find(self, *_args, **_kwargs):
        return list(self._documents)

    def drop(self):
        self.store.setdefault("dropped", []).append(self.name)

    def count_documents(self, _filter):
        return len(self._documents)


class _FakeDB:
    def __init__(self, documents=None):
        self.created = []
        self.store = {"docs": documents or {}}

    def create_collection(self, name, **opts):
        if name in [n for n, _ in self.created]:
            raise CollectionInvalid(f"{name} already exists")
        self.created.append((name, opts))

    def __getitem__(self, name):
        return _FakeCollection(name, self.store)


def _connector_with_fake_db(documents=None):
    conn = MongoConnector(_params())
    conn._client = {"testdb": _FakeDB(documents)}
    return conn


# ------------------------------------------------------- _split_top_level_args


def test_split_top_level_args_simple():
    assert _split_top_level_args('"a", "b"') == ['"a"', '"b"']


def test_split_top_level_args_does_not_split_inside_nested_object():
    args = '"EMPLOYEES", {"a": 1, "b": 2}'
    parts = _split_top_level_args(args)
    assert len(parts) == 2
    assert parts[0] == '"EMPLOYEES"'
    assert parts[1] == '{"a": 1, "b": 2}'


def test_split_top_level_args_ignores_comma_inside_string_value():
    # A very real shape: a column's "description" field commonly contains
    # something like "NUMBER(10,2) NOT NULL" -- that comma must not be
    # mistaken for an argument separator.
    args = '"EMPLOYEES", {"description": "NUMBER(10,2) NOT NULL"}'
    parts = _split_top_level_args(args)
    assert len(parts) == 2
    assert "NUMBER(10,2) NOT NULL" in parts[1]


def test_split_top_level_args_handles_pretty_printed_multiline_object():
    args = '"EMPLOYEES", {\n  "a": 1,\n  "b": {\n    "c": 2\n  }\n}'
    parts = _split_top_level_args(args)
    assert len(parts) == 2


def test_split_top_level_args_single_argument():
    assert _split_top_level_args('"counters"') == ['"counters"']


def test_split_top_level_args_empty():
    assert _split_top_level_args("") == []


# ------------------------------------------------------------ comment helpers


def test_is_pure_comment_block_true_for_dashes_only():
    assert _is_pure_comment_block("-- just a note\n-- another line") is True


def test_is_pure_comment_block_true_for_manual_placeholder():
    text = (
        "-- MANUAL CONVERSION REQUIRED for PROCEDURE P1\n"
        "-- MongoDB has no server-side stored-procedure/trigger equivalent.\n"
        "/*\nPROCEDURE p1 IS BEGIN NULL; END;\n*/"
    )
    assert _is_pure_comment_block(text) is True


def test_is_pure_comment_block_false_when_real_statement_present():
    text = '-- header\ndb.createCollection("T");'
    assert _is_pure_comment_block(text) is False


def test_strip_leading_comment_lines_drops_header_and_blank_lines():
    text = '-- Tables (1)\n\n-- more\n\ndb.createCollection("T")'
    assert _strip_leading_comment_lines(text) == 'db.createCollection("T")'


def test_strip_leading_comment_lines_no_op_when_no_leading_comment():
    text = 'db.createCollection("T")'
    assert _strip_leading_comment_lines(text) == text


# ---------------------------------------------------------------- schema_name


def test_schema_name_is_the_database_name():
    conn = MongoConnector(_params(database="mydb"))
    assert conn.schema_name == "mydb"


# -------------------------------------------------------------------- execute


def test_execute_raises_not_implemented_error():
    conn = MongoConnector(_params())
    try:
        conn.execute("SELECT 1")
        assert False, "expected NotImplementedError"
    except NotImplementedError as exc:
        assert "no SQL dialect" in str(exc)


# ----------------------------------------------------------------- execute_ddl


def test_execute_ddl_create_collection_with_validator():
    conn = _connector_with_fake_db()
    ddl = 'db.createCollection("EMPLOYEES", {"validator": {"$jsonSchema": {"bsonType": "object"}}});'
    conn.execute_ddl(ddl)
    assert conn.db.created == [("EMPLOYEES", {"validator": {"$jsonSchema": {"bsonType": "object"}}})]


def test_execute_ddl_create_collection_no_options():
    conn = _connector_with_fake_db()
    conn.execute_ddl('db.createCollection("counters");')
    assert conn.db.created == [("counters", {})]


def test_execute_ddl_create_collection_idempotent_on_rerun():
    conn = _connector_with_fake_db()
    ddl = 'db.createCollection("EMPLOYEES");'
    conn.execute_ddl(ddl)
    conn.execute_ddl(ddl)  # must not raise
    assert len(conn.db.created) == 1


def test_execute_ddl_create_index():
    conn = _connector_with_fake_db()
    ddl = 'db["EMPLOYEES"].createIndex({"EMP_ID": 1}, {"unique": true, "name": "PK_EMPLOYEES"});'
    conn.execute_ddl(ddl)
    name, keys, opts = conn.db.store["indexes"][0]
    assert name == "EMPLOYEES"
    assert keys == [("EMP_ID", 1)]
    assert opts == {"unique": True, "name": "PK_EMPLOYEES"}


def test_execute_ddl_update_one_for_sequence_counter():
    conn = _connector_with_fake_db()
    ddl = 'db["counters"].updateOne({"_id": "EMP_SEQ"}, {"$setOnInsert": {"seq": 1}}, {"upsert": true});'
    conn.execute_ddl(ddl)
    name, filter_, update, opts = conn.db.store["updates"][0]
    assert name == "counters"
    assert filter_ == {"_id": "EMP_SEQ"}
    assert update == {"$setOnInsert": {"seq": 1}}
    assert opts == {"upsert": True}


def test_execute_ddl_no_op_for_foreign_key_note_comment():
    conn = _connector_with_fake_db()
    conn.execute_ddl(
        "-- NOTE: ACCOUNT.CUSTOMER_ID references CUSTOMER.CUSTOMER_ID -- MongoDB has no "
        "foreign-key enforcement; this relationship must be maintained by application code."
    )
    assert conn.db.created == []


def test_execute_ddl_no_op_for_manual_conversion_placeholder():
    conn = _connector_with_fake_db()
    ddl = (
        "-- MANUAL CONVERSION REQUIRED for PROCEDURE RAISE_SALARY\n"
        "-- MongoDB has no server-side stored-procedure/trigger equivalent.\n"
        "/*\nPROCEDURE raise_salary IS BEGIN UPDATE t SET x = 1; END;\n*/"
    )
    conn.execute_ddl(ddl)  # must not raise
    assert conn.db.created == []


def test_execute_ddl_strips_section_header_prefix_before_a_real_statement():
    conn = _connector_with_fake_db()
    ddl = '-- Tables (1)\n\ndb.createCollection("EMPLOYEES");'
    conn.execute_ddl(ddl)
    assert conn.db.created == [("EMPLOYEES", {})]


def test_execute_ddl_raises_valueerror_for_unrecognized_statement():
    conn = _connector_with_fake_db()
    try:
        conn.execute_ddl("db.dropDatabase();")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Unrecognized MongoDB DDL statement" in str(exc)


def test_execute_ddl_raises_valueerror_for_unrecognized_collection_method():
    conn = _connector_with_fake_db()
    try:
        conn.execute_ddl('db["EMPLOYEES"].renameCollection("STAFF");')
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Unrecognized MongoDB collection method" in str(exc)


def test_execute_ddl_drop_drops_the_collection():
    # Emitted by ddl_generator.generate_rollback_ddl for a MongoDB target
    # (see _drop_table_ddl's MongoDB branch) -- execute_ddl must recognize
    # and actually perform it, not just tolerate it as a no-op comment.
    conn = _connector_with_fake_db()
    conn.execute_ddl('db["EMPLOYEES"].drop();')
    assert conn.db.store.get("dropped") == ["EMPLOYEES"]


# ---------------------------------------------------------------- insert_batch


def test_insert_batch_builds_documents_from_columns_and_rows():
    conn = _connector_with_fake_db()
    conn.insert_batch("EMPLOYEES", ["EMP_ID", "NAME"], [(1, "Alice"), (2, "Bob")])
    name, docs = conn.db.store["inserts"][0]
    assert name == "EMPLOYEES"
    assert docs == [{"EMP_ID": 1, "NAME": "Alice"}, {"EMP_ID": 2, "NAME": "Bob"}]


def test_insert_batch_no_op_on_empty_rows():
    conn = _connector_with_fake_db()
    conn.insert_batch("EMPLOYEES", ["EMP_ID"], [])
    assert "inserts" not in conn.db.store


# -------------------------------------------------------- module-level helpers


class _FakeObjectId:
    def __init__(self, hex_value):
        self._hex = hex_value

    def __str__(self):
        return self._hex


class _FakeDecimal128:
    def __init__(self, decimal_value):
        self._value = decimal_value

    def to_decimal(self):
        return self._value


def test_get_path_walks_nested_dicts():
    from tgdatabridge.db.mongo_connector import _get_path
    doc = {"address": {"city": "Springfield", "geo": {"lat": 1.5}}}
    assert _get_path(doc, "address.city") == "Springfield"
    assert _get_path(doc, "address.geo.lat") == 1.5


def test_get_path_returns_none_for_missing_or_non_dict_intermediate():
    from tgdatabridge.db.mongo_connector import _get_path
    assert _get_path({"a": 1}, "a.b") is None
    assert _get_path({}, "a.b") is None
    assert _get_path(None, "a") is None


def test_get_path_single_segment_reads_top_level_key():
    from tgdatabridge.db.mongo_connector import _get_path
    assert _get_path({"_id": 42}, "_id") == 42


def test_to_portable_value_converts_objectid_to_str():
    from tgdatabridge.db.mongo_connector import _to_portable_value
    _FakeObjectId.__name__ = "ObjectId"
    assert _to_portable_value(_FakeObjectId("abc123")) == "abc123"


def test_to_portable_value_converts_decimal128_via_to_decimal():
    from tgdatabridge.db.mongo_connector import _to_portable_value
    _FakeDecimal128.__name__ = "Decimal128"
    result = _to_portable_value(_FakeDecimal128("9.99"))
    assert result == "9.99"


def test_to_portable_value_passthrough_for_plain_types():
    from tgdatabridge.db.mongo_connector import _to_portable_value
    assert _to_portable_value(5) == 5
    assert _to_portable_value("x") == "x"
    assert _to_portable_value(None) is None


def test_to_portable_value_serializes_stray_dict_or_list_as_json():
    from tgdatabridge.db.mongo_connector import _to_portable_value
    import json
    assert json.loads(_to_portable_value({"a": 1})) == {"a": 1}
    assert json.loads(_to_portable_value([1, 2])) == [1, 2]


# ------------------------------------------------------- fetch_batches_table


def _make_table(name, columns, source_collection=None, source_array_path=None):
    from tgdatabridge.core.schema_model import Column, Table
    return Table(
        name=name, schema="APP",
        columns=[Column(name=c, data_type="VARCHAR2(100)") for c in columns],
        source_collection=source_collection or name, source_array_path=source_array_path,
    )


def test_fetch_batches_table_top_level_reads_real_collection():
    docs = {
        "EMPLOYEES": [
            {"_id": 1, "NAME": "Alice", "ADDRESS": {"CITY": "Springfield"}},
            {"_id": 2, "NAME": "Bob", "ADDRESS": {"CITY": "Shelbyville"}},
        ],
    }
    conn = _connector_with_fake_db(docs)
    table = _make_table("EMPLOYEES", ["_id", "NAME", "ADDRESS.CITY"])
    batches = list(conn.fetch_batches_table(table, batch_size=5000))
    assert len(batches) == 1
    columns, rows = batches[0]
    assert columns == ["_id", "NAME", "ADDRESS.CITY"]
    assert rows == [(1, "Alice", "Springfield"), (2, "Bob", "Shelbyville")]


def test_fetch_batches_table_respects_batch_size():
    docs = {"T": [{"_id": i} for i in range(5)]}
    conn = _connector_with_fake_db(docs)
    table = _make_table("T", ["_id"])
    batches = list(conn.fetch_batches_table(table, batch_size=2))
    assert [len(rows) for _cols, rows in batches] == [2, 2, 1]


def test_fetch_batches_table_child_table_unwinds_array_of_scalars():
    docs = {
        "ORDERS": [
            {"_id": 100, "TAGS": ["a", "b"]},
            {"_id": 101, "TAGS": ["c"]},
        ],
    }
    conn = _connector_with_fake_db(docs)
    child = _make_table(
        "ORDERS_TAGS", ["ORDERS_TAGS_ID", "ORDERS_ID", "value"],
        source_collection="ORDERS", source_array_path="TAGS",
    )
    columns, rows = next(conn.fetch_batches_table(child, batch_size=5000))
    assert columns == ["ORDERS_TAGS_ID", "ORDERS_ID", "value"]
    assert rows == [(1, 100, "a"), (2, 100, "b"), (3, 101, "c")]


def test_fetch_batches_table_child_table_unwinds_array_of_subdocuments():
    docs = {
        "ORDERS": [
            {"_id": 100, "ITEMS": [{"SKU": "X1", "QTY": 2}, {"SKU": "X2", "QTY": 1}]},
        ],
    }
    conn = _connector_with_fake_db(docs)
    child = _make_table(
        "ORDERS_ITEMS", ["ORDERS_ITEMS_ID", "ORDERS_ID", "SKU", "QTY"],
        source_collection="ORDERS", source_array_path="ITEMS",
    )
    columns, rows = next(conn.fetch_batches_table(child, batch_size=5000))
    assert columns == ["ORDERS_ITEMS_ID", "ORDERS_ID", "SKU", "QTY"]
    assert rows == [(1, 100, "X1", 2), (2, 100, "X2", 1)]


def test_fetch_batches_table_child_table_skips_documents_where_array_missing():
    docs = {"ORDERS": [{"_id": 100}, {"_id": 101, "TAGS": ["x"]}]}
    conn = _connector_with_fake_db(docs)
    child = _make_table(
        "ORDERS_TAGS", ["ORDERS_TAGS_ID", "ORDERS_ID", "value"],
        source_collection="ORDERS", source_array_path="TAGS",
    )
    columns, rows = next(conn.fetch_batches_table(child, batch_size=5000))
    assert rows == [(1, 101, "x")]


def test_fetch_batches_table_no_documents_yields_nothing():
    conn = _connector_with_fake_db({"EMPTY": []})
    table = _make_table("EMPTY", ["_id"])
    batches = list(conn.fetch_batches_table(table, batch_size=5000))
    assert batches == []


# --------------------------------------------------------------- count_rows


def test_count_rows_returns_document_count():
    docs = {"EMPLOYEES": [{"_id": 1}, {"_id": 2}, {"_id": 3}]}
    conn = _connector_with_fake_db(docs)
    assert conn.count_rows("EMPLOYEES") == 3


def test_count_rows_zero_for_empty_collection():
    conn = _connector_with_fake_db({"EMPLOYEES": []})
    assert conn.count_rows("EMPLOYEES") == 0


# ------------------------------------------------------------ checksum_rows


def test_checksum_rows_matches_validation_table_checksum():
    from tgdatabridge.core.validation import row_checksum

    docs = {"EMPLOYEES": [{"_id": 1, "NAME": "Alice"}, {"_id": 2, "NAME": "Bob"}]}
    conn = _connector_with_fake_db(docs)
    checksum = conn.checksum_rows("EMPLOYEES", ["_id", "NAME"])
    expected = row_checksum((1, "Alice")) ^ row_checksum((2, "Bob"))
    assert checksum == expected


def test_checksum_rows_is_order_independent():
    docs_a = {"EMPLOYEES": [{"_id": 1, "NAME": "Alice"}, {"_id": 2, "NAME": "Bob"}]}
    docs_b = {"EMPLOYEES": [{"_id": 2, "NAME": "Bob"}, {"_id": 1, "NAME": "Alice"}]}
    conn_a = _connector_with_fake_db(docs_a)
    conn_b = _connector_with_fake_db(docs_b)
    assert conn_a.checksum_rows("EMPLOYEES", ["_id", "NAME"]) == conn_b.checksum_rows("EMPLOYEES", ["_id", "NAME"])


# --------------------------------------------------------------- TLS / SSL


def test_client_kwargs_have_no_tls_options_when_tls_is_off():
    conn = MongoConnector(_params())
    kwargs = conn._client_kwargs()
    assert "tls" not in kwargs
    assert "tlsCAFile" not in kwargs


def test_client_kwargs_enable_tls_and_ca_file():
    conn = MongoConnector(_params(tls=TlsConfig(enabled=True, ca_cert_path="/ca.pem")))
    kwargs = conn._client_kwargs()
    assert kwargs["tls"] is True
    assert kwargs["tlsCAFile"] == "/ca.pem"
    assert kwargs["tlsAllowInvalidCertificates"] is False
    assert kwargs["tlsAllowInvalidHostnames"] is False


def test_client_kwargs_allow_invalid_certificates_when_verify_cert_is_off():
    conn = MongoConnector(_params(tls=TlsConfig(enabled=True, verify_cert=False)))
    kwargs = conn._client_kwargs()
    assert kwargs["tlsAllowInvalidCertificates"] is True


def test_client_kwargs_allow_invalid_hostnames_when_verify_hostname_is_off():
    conn = MongoConnector(_params(
        tls=TlsConfig(enabled=True, verify_cert=True, verify_hostname=False)))
    kwargs = conn._client_kwargs()
    assert kwargs["tlsAllowInvalidHostnames"] is True


def test_client_kwargs_allow_invalid_hostnames_when_tunneled():
    """Unlike Oracle/MySQL/DB2, PyMongo can check the CA signature while
    skipping the hostname match -- exactly what a tunneled connection
    needs, since it dials 127.0.0.1."""
    conn = MongoConnector(_params(
        host="127.0.0.1",
        tls=TlsConfig(enabled=True, verify_hostname=True, server_host_override="mongo.internal"),
    ))
    kwargs = conn._client_kwargs()
    assert kwargs["tlsAllowInvalidCertificates"] is False
    assert kwargs["tlsAllowInvalidHostnames"] is True


def test_client_cert_key_file_combines_cert_and_key_into_one_pem(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n")
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\nBBB\n-----END PRIVATE KEY-----\n")
    tls = TlsConfig(enabled=True, client_cert_path=str(cert), client_key_path=str(key))

    combined_path = MongoConnector._client_cert_key_file(tls)

    assert combined_path is not None
    combined = open(combined_path, "rb").read()
    assert b"BEGIN CERTIFICATE" in combined
    assert b"BEGIN PRIVATE KEY" in combined
    assert combined.index(b"CERTIFICATE") < combined.index(b"PRIVATE KEY")


def test_client_cert_key_file_none_when_mutual_tls_not_configured():
    assert MongoConnector._client_cert_key_file(TlsConfig(enabled=True)) is None


def test_client_kwargs_include_the_combined_pem_when_mutual_tls_is_set(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_bytes(b"cert-bytes")
    key.write_bytes(b"key-bytes")
    conn = MongoConnector(_params(tls=TlsConfig(
        enabled=True, client_cert_path=str(cert), client_key_path=str(key),
        client_key_password="secret")))
    kwargs = conn._client_kwargs()
    assert "tlsCertificateKeyFile" in kwargs
    assert kwargs["tlsCertificateKeyFilePassword"] == "secret"
