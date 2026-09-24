# Integration tests

These are the tests that talk to a real database server.

## Why they exist

Before this suite, **1 of 54 test modules used a real database**. The
other 53 used fakes, which is what lets 1,400-odd tests run in four
seconds with nothing installed — a trade worth making, with one blind
spot: a fake cursor accepts values a server rejects.

Three defects reached users through that blind spot, each while the unit
suite was entirely green:

| Symptom the user saw | Cause | Unit tests passing at the time |
|---|---|---|
| `0 rows migrated`, every table failed | binary `COPY` inferred `numeric` for Python `int`; int4/int8 reject it | 1,348 |
| `1054 Unknown column 'employee_id'` | a pre-existing target table that `CREATE TABLE IF NOT EXISTS` declined to touch | 1,390 |
| `Unvalidated` on a correct table | `DATE` widened to a timestamp, so source and target checksums disagreed | 1,403 |

The first run of this harness found two more, both invisible to 1,408
passing unit tests:

- **A false `Unvalidated` on every table with a `DATE` column**, on
  PostgreSQL *and* MySQL. The widening is documented and intended; the
  checksum was calling it corruption. Fixed in
  `tgdatabridge/core/validation.py` (`_canonical`), algorithm bumped to
  `blake2b64-v3`.
- **Silent loss of every microsecond on a MySQL target.** The pivot's
  `TIMESTAMP(6)` mapped to a bare `DATETIME`, which stores whole seconds
  only. Real data loss, surfacing to the user only as a checksum
  mismatch. Fixed in `tgdatabridge/core/type_mapping.py`; MySQL is the only one
  of the five SQL targets with this default.

## Running them

```
Run Integration Tests.bat            # Windows: start containers, test, stop
./run-integration-tests.sh           # Linux/macOS, same thing
```

Both take `--keep` (leave the containers up) and `--existing` (use
servers you already have — edit the connection strings at the top).

To run against one server by hand:

```
set TGSCT_IT_POSTGRES=host=127.0.0.1 port=5433 dbname=tgtest user=tg password=tgpass
python -m pytest tests/integration -v
```

Nothing here runs during an ordinary `pytest tests/` on a machine with no
server configured, so the fast suite stays fast.

## How engines are discovered

One environment variable per engine, each a space-separated
`key=value` connection string:

| Engine | Variable |
|---|---|
| PostgreSQL | `TGSCT_IT_POSTGRES` |
| MySQL | `TGSCT_IT_MYSQL` |
| SQL Server | `TGSCT_IT_SQLSERVER` |
| Db2 | `TGSCT_IT_DB2` |
| MongoDB | `TGSCT_IT_MONGODB` |
| Oracle | `TGSCT_IT_ORACLE` |

An engine whose variable is unset produces no test parameter at all,
rather than a skipped one — a suite that reports "30 skipped" on every
machine trains people to ignore skips, and the one time it matters they
will. Instead pytest prints, in its header, exactly which engines were
reached and which were not:

```
integration engines reachable: PostgreSQL, MySQL
integration engines NOT configured (tests skipped): SQL Server, DB2, MongoDB, Oracle
```

Db2 is in the compose file's `heavy` profile (multi-gigabyte image,
minutes to start, needs `privileged: true`). Oracle is not in the compose
file at all — no image is redistributable on terms this project can
accept on a user's behalf — so it is picked up only from a server you
already have.

## Safety

Every test creates a scratch namespace named `tgdatabridge_it_<pid>_<n>`, works
only inside it, and drops it afterwards whether the test passed or not —
a schema on PostgreSQL, SQL Server, Db2 and Oracle, a database on MySQL
and MongoDB. Nothing outside that namespace is read or written.

That makes it safe to point at a shared *development* server. It is not
safe to point at anything you care about, and the compose file
deliberately declares no volumes so `down` really does take the data with
it.

Oracle is the exception: an Oracle "schema" is a user account, and
creating one needs privileges this harness has no business assuming, so
Oracle runs against whatever schema the DSN's user already owns and
creates its tables there.

## What the files are

| File | What it holds |
|---|---|
| `engines.py` | discovery, and per-engine scratch-namespace create/drop |
| `conftest.py` | parameterisation over reachable engines; the namespace/target/reader fixtures |
| `fixture_schema.py` | the one table every conformance test migrates, and why each column is in it |
| `harness.py` | the in-memory source, DDL application, read-back, cross-engine value comparison |
| `test_conformance.py` | the full arc per engine: DDL → apply → migrate → validate → read back → drop |
| `test_known_regressions.py` | every defect that reached a user, pinned per engine |

## Adding an engine

Add its label to `ENV_VARS`, `_NAMESPACE_OPS` and `_NAMESPACE_KIND` in
`engines.py`, and a service to `docker-compose.integration.yml`. Nothing
in the test files names an engine except where behaviour genuinely
differs, and there is exactly one such place today
(`_DELIMITER_NAMES` in `test_known_regressions.py`, which needs to know
each dialect's own quoting character).

There are deliberately **no per-engine carve-outs inside assertions**.
There was one — MySQL's `when_exact` compared with microseconds stripped
— and it was concealing the truncation bug listed above. An exception
written into an assertion is a bug with a comment in front of it.
