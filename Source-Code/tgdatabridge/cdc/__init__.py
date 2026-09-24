"""Change-data-capture integration -- SCALE.md section 2.1.

A 1 TB migration with a cutover window of a few hours cannot be a
stop-the-world copy, however fast the copy gets: the bulk load alone is
hours, and index/constraint rebuild is hours more. The only shape that
fits is bulk-load ahead of time while the source stays live, capture the
changes made meanwhile, and apply a small delta at cutover.

This package deliberately **integrates** a CDC engine rather than
implementing one. Capturing changes out of Oracle is a large, subtle
problem -- LogMiner or XStream, supplemental logging configuration, LOB
and DDL change handling, transaction boundary ordering, exactly-once
apply semantics -- and building it badly is worse than not building it.
Debezium already does it, is open source, and is battle-tested.

What this tool contributes is the part Debezium doesn't do and this tool
is uniquely placed to do, because it has already introspected the schema:

  - `config.py`     -- generates the Kafka Connect connector JSON from
                       the real schema, so the include lists, topic
                       prefix and snapshot mode are correct by
                       construction rather than hand-maintained.
  - `preflight.py`  -- checks the Oracle-side prerequisites that
                       otherwise cause Debezium to fail confusingly hours
                       into a run, and prints the exact SQL to fix each.
  - `connect_client.py` -- a thin Kafka Connect REST client (stdlib only).
  - `orchestrator.py`   -- the start/load/drain ordering, which is the
                       one piece where getting it wrong silently loses
                       data.

Nothing here is a Kafka consumer: applying the captured changes to
PostgreSQL is the Debezium JDBC sink connector's job, not this tool's.
"""
