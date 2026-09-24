"""MongoDB connector, built on PyMongo -- usable as both a target and a
source.

`execute()` -- the free-form "run this SQL query" method every other
connector implements for source-side introspection/data migration --
still deliberately raises NotImplementedError here: there is no SQL
dialect for a MongoDB-side query to even be written in, target- and
source-side introspection both work by reading collections directly off
the `db` property instead (see tgdatabridge.core.target_introspector.
introspect_target_mongodb and tgdatabridge.core.mongo_source_introspector.
introspect_schema). Source-side *data migration* has its own duck-typed
method below, `fetch_batches_table()`, rather than reusing `execute()`
or the SQL-string-based `fetch_batches()` every other connector
implements -- see that method's own docstring for why a SQL string can't
express what a MongoDB-sourced migration needs.

`execute_ddl()` is the interesting piece: ddl_generator.py never emits real
MongoDB shell syntax for this method to hand off to a server-side SQL
parser (there isn't one) -- instead it emits a small, self-controlled
subset of mongosh-flavored JavaScript built entirely from `json.dumps(...)`,
using exactly three statement shapes:

  db.createCollection("name", {...options...})   -- no collection prefix
  db["collection"].createIndex({...}, {...})      -- always bracket notation
  db["collection"].updateOne({...}, {...}, {...})
  db["collection"].drop()                         -- emitted by
                                                       ddl_generator.generate_rollback_ddl

This module parses exactly those three shapes back out via regex + a
top-level-comma-respecting argument splitter (`_split_top_level_args`) and
re-issues them as real PyMongo calls. It is deliberately narrow -- it
raises ValueError on anything else -- rather than attempting to be a
general JavaScript interpreter, because ddl_generator.py is the only thing
that ever produces the text this method receives.

A statement that is *purely* commentary (a "-- NOTE: ..." foreign-key
documentation line with nothing else, or a "-- MANUAL CONVERSION REQUIRED"
placeholder wrapping the original routine/view source in `/* ... */` --
see plsql_converter.convert_routine's MongoDB branch and
generate_view_ddl's MongoDB branch) is recognized and treated as a no-op,
never raising ValueError -- there is nothing there for a MongoDB target to
actually do.

`count_rows()`/`checksum_rows()` -- added for tgdatabridge.core.validation's
post-migration validation and tgdatabridge.core.migrator's dry-run planning --
read documents directly off the `db` property, the same way everything
else in this module that isn't `execute_ddl()` does; there is no COUNT(*)
to write here, `count_documents({})` is the direct equivalent.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterable, Iterator, List, Optional, Tuple

from tgdatabridge.core.schema_model import Table
from tgdatabridge.db.base import ConnectionParams

_JSON_STRING = r'"(?:[^"\\]|\\.)*"'
_TOP_LEVEL_RE = re.compile(r"^db\.createCollection\((.*)\)$", re.DOTALL)
_COLLECTION_RE = re.compile(rf"^db\[({_JSON_STRING})\]\.(\w+)\((.*)\)$", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _split_top_level_args(args_str: str) -> List[str]:
    """Split a `json.dumps(...)`-built argument list on only its top-level
    commas -- i.e. the commas separating whole arguments to db.createCollection
    / db[...].createIndex / db[...].updateOne, not any comma nested inside a
    JSON object/array argument (very common: ddl_generator pretty-prints the
    $jsonSchema validator options with `indent=2`, and even a single-line
    JSON value like a column's "description" field routinely contains a
    literal comma, e.g. "NUMBER(10,2) NOT NULL"). Tracks bracket/brace depth
    and JSON string-literal state (including backslash escapes) character by
    character; newlines inside a pretty-printed object are not special and
    pass straight through."""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    in_str = False
    escaped = False
    for ch in args_str:
        if in_str:
            current.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            current.append(ch)
            continue
        if ch in "{[":
            depth += 1
            current.append(ch)
            continue
        if ch in "}]":
            depth -= 1
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    tail = "".join(current)
    if tail.strip():
        parts.append(tail)
    return [p.strip() for p in parts]


def _is_pure_comment_block(stmt: str) -> bool:
    """True if `stmt` has no real content once every "--"-prefixed line and
    every "/* ... */" block is removed -- i.e. it is entirely a foreign-key
    documentation note and/or a MANUAL-CONVERSION-REQUIRED placeholder's
    wrapped original source, with no actual db.___(...) call in it."""
    text = stmt.strip()
    lines = [ln for ln in text.split("\n") if not ln.strip().startswith("--")]
    text = "\n".join(lines)
    text = _BLOCK_COMMENT_RE.sub("", text)
    return text.strip() == ""


def _strip_leading_comment_lines(stmt: str) -> str:
    """Drop any "--"-prefixed (or blank) lines glued onto the front of a
    real statement -- e.g. a foreign-key "-- NOTE: ..." documentation line,
    or one of generate_schema_ddl's own "-- Tables (N)"/"-- Views (N)"/etc.
    section-header comments, both of which have no terminating ';' of their
    own and so merge with whatever real db.createCollection(...)/
    db[...].___(...) statement comes right after them (see
    split_sql_statements' module docstring for why comment-only blocks with
    no ';' of their own behave this way). Blank lines between/after such
    comments are skipped too, so a run of "-- header\\n\\n-- header\\n\\ndb...."
    is fully stripped down to just the real statement."""
    lines = stmt.split("\n")
    while lines and (lines[0].strip() == "" or lines[0].strip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def _get_path(document: Optional[dict], dotted_path: str):
    """Walk `dotted_path` (e.g. "address.city") into `document`, returning
    None the moment any level is missing or isn't itself a dict -- the
    same "just return null" tolerance a mismatched document (one that
    doesn't quite match what was inferred from the sample) needs, rather
    than raising."""
    current = document
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _to_portable_value(value):
    """Convert a raw PyMongo/BSON value into something every target
    connector's `insert_batch` (ultimately a plain parameterized SQL
    INSERT for every non-Mongo target) can bind as a query parameter.
    Duck-typed by class name rather than `isinstance` against the real
    `bson` classes -- mirrors mongo_source_introspector._bson_type_name's
    own reasoning: this must work whether pymongo/bson is actually
    installed or not."""
    if value is None:
        return None
    type_name = type(value).__name__
    if type_name == "ObjectId":
        return str(value)
    if type_name == "Decimal128":
        to_decimal = getattr(value, "to_decimal", None)
        return to_decimal() if callable(to_decimal) else str(value)
    if type_name == "Binary":
        return bytes(value)
    if isinstance(value, (dict, list)):
        # A nested object/array value reaching here would mean a document
        # doesn't match the shape mongo_source_introspector inferred from
        # its sample (which only ever emits scalar-leaf columns) --
        # serialize it as JSON text rather than handing a target driver a
        # Python object it has no idea how to bind.
        return json.dumps(value, default=str)
    return value


def _row_for_document(document: dict, columns: List[str]) -> tuple:
    return tuple(_to_portable_value(_get_path(document, col)) for col in columns)


class MongoConnector:
    def __init__(self, params: ConnectionParams):
        self.params = params
        self._client = None

    def _client_kwargs(self) -> dict:
        """Everything pymongo.MongoClient() needs beyond host/port, built
        without touching the driver -- testable with no pymongo installed
        (see this module's own docstring for why pymongo is only ever
        imported lazily, inside connect() itself), mirroring every other
        connector's own `_connect_kwargs`.

        TLS/SSL: PyMongo's `tls`/`tlsCAFile` map straight onto
        TlsConfig's `enabled`/`ca_cert_path`. Certificate-chain
        verification and hostname verification are two *separate*
        booleans here (`tlsAllowInvalidCertificates`,
        `tlsAllowInvalidHostnames`) -- unlike Oracle's, MySQL's and Db2's
        drivers, PyMongo can be told to check the CA signature while
        skipping the hostname match, which is exactly what a tunneled
        connection needs (the tunnel dials 127.0.0.1, which will never
        match the certificate's real name). A client certificate for
        mutual TLS has to be one PEM file containing both the certificate
        and its private key -- `_client_cert_key_file` builds that file
        when this connector is given the two as separate paths, which is
        how every other engine here and the connection dialog take them.
        """
        kwargs: dict = {"serverSelectionTimeoutMS": 5000}
        if self.params.username:
            kwargs["username"] = self.params.username
        if self.params.password:
            kwargs["password"] = self.params.password
        tls = self.params.tls
        if tls and tls.enabled:
            kwargs["tls"] = True
            if tls.ca_cert_path:
                kwargs["tlsCAFile"] = tls.ca_cert_path
            kwargs["tlsAllowInvalidCertificates"] = not tls.verify_cert
            can_check_hostname = tls.effective_hostname(self.params.host) == self.params.host
            kwargs["tlsAllowInvalidHostnames"] = not (tls.verify_hostname and can_check_hostname)
            client_pem = self._client_cert_key_file(tls)
            if client_pem:
                kwargs["tlsCertificateKeyFile"] = client_pem
                if tls.client_key_password:
                    kwargs["tlsCertificateKeyFilePassword"] = tls.client_key_password
        return kwargs

    @staticmethod
    def _client_cert_key_file(tls) -> Optional[str]:
        """PyMongo's `tlsCertificateKeyFile` wants one PEM with both the
        certificate and the private key concatenated -- unlike psycopg's
        sslcert/sslkey or mysql-connector's ssl_cert/ssl_key, which take
        them as two separate files. Combines them into a temp file
        (mode 0o600, this process's temp directory) the first time this
        connector needs it; the file lives as long as the process, the
        same lifetime as the connector itself and cheap enough not to
        bother cleaning up sooner. None when mutual TLS isn't configured,
        so `_client_kwargs` above leaves `tlsCertificateKeyFile` unset
        entirely rather than pointing at an empty file."""
        if not (tls.client_cert_path and tls.client_key_path):
            return None
        import stat
        import tempfile

        cert_bytes = open(tls.client_cert_path, "rb").read()
        key_bytes = open(tls.client_key_path, "rb").read()
        fd, path = tempfile.mkstemp(prefix="tgdatabridge_mongo_tls_", suffix=".pem")
        try:
            os.write(fd, cert_bytes)
            if not cert_bytes.endswith(b"\n"):
                os.write(fd, b"\n")
            os.write(fd, key_bytes)
        finally:
            os.close(fd)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return path

    def connect(self) -> None:
        import pymongo  # lazy import so the GUI can start without the driver installed

        self._client = pymongo.MongoClient(self.params.host, self.params.port, **self._client_kwargs())
        # MongoClient itself is lazy (no network I/O happens above) -- force
        # one real round-trip now so a bad host/port/credential surfaces
        # immediately here, matching every other engine's connect().
        self._client.admin.command("ping")

    @property
    def db(self):
        """The PyMongo Database this connector targets. Exposed directly
        (unlike every other connector's SQL-string-based `execute()`)
        because introspection and DDL execution both need to work with
        real collection objects, not query text -- see
        tgdatabridge.core.target_introspector.introspect_target_mongodb."""
        return self._client[self.params.database]

    @property
    def schema_name(self) -> str:
        # MongoDB has no separate "schema" concept distinct from the
        # database itself (same reasoning as MySQL's own schema_name-less
        # design) -- the database name doubles as what
        # introspect_target_mongodb reads collections from.
        return self.params.database

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            return True, "Connected"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            self.close()

    def execute(self, sql: str, params: Optional[dict] = None) -> Iterable[tuple]:
        raise NotImplementedError(
            "MongoConnector.execute() is not implemented -- there is no SQL dialect for a MongoDB "
            "query to even be written in. Both target-side and source-side introspection read "
            "collections directly via the `db` property instead of a SQL query -- see "
            "tgdatabridge.core.target_introspector.introspect_target_mongodb and "
            "tgdatabridge.core.mongo_source_introspector.introspect_schema -- and source-side data "
            "migration uses fetch_batches_table() below instead of a SQL-string fetch_batches()."
        )

    def fetch_batches_table(
        self, table: Table, batch_size: int = 5000,
    ) -> Iterator[Tuple[List[str], List[tuple]]]:
        """The MongoDB-source counterpart of every other connector's
        SQL-string `fetch_batches(sql, batch_size)` -- migrator.
        migrate_table() prefers this method over building/using a SQL
        string whenever the source connector defines it (a purely
        additive, duck-typed dispatch; every other connector is
        unaffected -- see migrator.migrate_table's own comment).

        A SQL string can't express what a MongoDB-sourced migration
        needs: for a synthesized child table (`table.source_array_path`
        is not None -- see mongo_source_introspector._build_child_table),
        the rows to migrate don't exist as documents in their own right
        at all, they only exist by unwinding one array field out of the
        *parent* collection's documents. So this reads real documents off
        `table.source_collection` directly and, for a child table, unwinds
        `table.source_array_path` out of each one, yielding
        `(columns, rows)` batches exactly like every other connector's
        `fetch_batches` -- migrator.migrate_table then hands each batch to
        `target.insert_batch()` completely unchanged either way.

        The synthesized surrogate primary key for a child table's rows
        (see mongo_source_introspector._build_child_table's `<child>_ID`
        column) has no real value in MongoDB to read -- there is nothing
        to migrate *from* for it -- so it's generated here as a simple
        incrementing counter, scoped to one call of this method (i.e. one
        full migration of one child table)."""
        columns = [c.name for c in table.columns]
        collection = self.db[table.source_collection or table.name]

        if table.source_array_path is None:
            batch: List[tuple] = []
            for document in collection.find({}):
                batch.append(_row_for_document(document, columns))
                if len(batch) >= batch_size:
                    yield columns, batch
                    batch = []
            if batch:
                yield columns, batch
            return

        # Child table: columns[0] is always the synthesized surrogate PK,
        # columns[1] the foreign key back to the parent's _id, and
        # everything after that is either a single "value" column (array
        # of scalars) or one column per item key (array of sub-documents)
        # -- exactly the layout mongo_source_introspector._build_child_table
        # built, mirrored here so the values line up with those columns.
        value_columns = columns[2:]
        is_scalar_array = value_columns == ["value"]
        batch = []
        next_id = 1
        for document in collection.find({}):
            items = _get_path(document, table.source_array_path)
            if not isinstance(items, list):
                continue
            parent_id = _to_portable_value(document.get("_id"))
            for item in items:
                if is_scalar_array:
                    row = (next_id, parent_id, _to_portable_value(item))
                else:
                    row = (next_id, parent_id, *_row_for_document(item if isinstance(item, dict) else {}, value_columns))
                batch.append(row)
                next_id += 1
                if len(batch) >= batch_size:
                    yield columns, batch
                    batch = []
        if batch:
            yield columns, batch

    def execute_ddl(self, sql: str) -> None:
        import pymongo

        stmt = sql.strip()
        if stmt.endswith(";"):
            stmt = stmt[:-1].rstrip()
        stmt = _strip_leading_comment_lines(stmt)
        if not stmt or _is_pure_comment_block(stmt):
            return

        m = _TOP_LEVEL_RE.match(stmt)
        if m:
            parts = _split_top_level_args(m.group(1))
            if not parts:
                raise ValueError("db.createCollection(...) called with no arguments")
            name = json.loads(parts[0])
            options = json.loads(parts[1]) if len(parts) > 1 else {}
            try:
                if options:
                    self.db.create_collection(name, **options)
                else:
                    self.db.create_collection(name)
            except pymongo.errors.CollectionInvalid:
                # "Collection already exists" -- re-applying the same DDL
                # against a target that's already been set up is a no-op,
                # the same idempotent-DDL tolerance every other engine's own
                # "IF NOT EXISTS"-equivalent guard gives.
                pass
            return

        m = _COLLECTION_RE.match(stmt)
        if m:
            coll_name = json.loads(m.group(1))
            method = m.group(2)
            parts = _split_top_level_args(m.group(3))
            args = [json.loads(p) for p in parts]
            collection = self.db[coll_name]
            if method == "createIndex":
                keys = list(args[0].items()) if args else []
                opts = args[1] if len(args) > 1 else {}
                collection.create_index(keys, **opts)
                return
            if method == "updateOne":
                filter_ = args[0] if len(args) > 0 else {}
                update = args[1] if len(args) > 1 else {}
                opts = args[2] if len(args) > 2 else {}
                collection.update_one(filter_, update, **opts)
                return
            if method == "drop":
                # Emitted by ddl_generator.generate_rollback_ddl -- rolling
                # back a table's CREATE (a real MongoDB collection) means
                # dropping that collection outright. drop() on a
                # collection that doesn't exist is already a silent no-op
                # in PyMongo/MongoDB itself, so no existence check is
                # needed here the way createCollection's ValueError-on-
                # duplicate needed one above.
                collection.drop()
                return
            raise ValueError(f"Unrecognized MongoDB collection method: {method}")

        raise ValueError(f"Unrecognized MongoDB DDL statement: {stmt[:200]!r}")

    def insert_batch(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        # Field names are used exactly as generate_table_ddl_mongodb built
        # its $jsonSchema `properties` keys: the column's original
        # (Oracle-cased) name, unmodified -- see that function's docstring.
        # Documents must use the same casing to actually validate against
        # the collection's validator.
        docs = [dict(zip(columns, row)) for row in rows]
        self.db[table].insert_many(docs)

    def count_rows(self, table: str, schema: Optional[str] = None) -> int:
        """Independent post-migration row count -- see
        tgdatabridge.core.validation. `schema` is accepted (matching every other
        connector's signature) but unused, same reasoning as schema_name
        above. Only meaningful for a real collection -- a source-side
        synthesized child table (Table.source_array_path is not None; see
        mongo_source_introspector._build_child_table) has no collection of
        its own to count when this is called against a *source* Mongo
        connector, only against a *target* one after migrate_table has
        actually created and populated the real target collection for it."""
        return self.db[table].count_documents({})

    def checksum_rows(
        self, table: str, columns: List[str], schema: Optional[str] = None, sample_size: Optional[int] = None,
    ) -> int:
        """Order-independent checksum of `table`'s documents -- see
        tgdatabridge.core.validation.table_checksum. Reuses _row_for_document so
        the values hashed here line up exactly with what migrate_table
        streamed into insert_batch for the same `columns`."""
        from tgdatabridge.core.validation import row_checksum

        cursor = self.db[table].find({})
        if sample_size:
            cursor = cursor.limit(int(sample_size))
        total = 0
        for document in cursor:
            total ^= row_checksum(_row_for_document(document, columns))
        return total
