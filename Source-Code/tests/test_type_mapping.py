from tgdatabridge.core import type_mapping


def test_number_small_integer_postgres():
    t, issues = type_mapping.to_postgres("NUMBER(3)")
    assert t == "SMALLINT"
    assert issues == []


def test_number_integer_postgres():
    t, issues = type_mapping.to_postgres("NUMBER(7)")
    assert t == "INTEGER"


def test_number_bigint_postgres():
    t, issues = type_mapping.to_postgres("NUMBER(15)")
    assert t == "BIGINT"


def test_number_with_scale_postgres():
    t, issues = type_mapping.to_postgres("NUMBER(10,2)")
    assert t == "NUMERIC(10,2)"


def test_number_no_precision_postgres_warns():
    t, issues = type_mapping.to_postgres("NUMBER")
    assert t == "NUMERIC"
    assert any(i.severity == "warning" for i in issues)


def test_varchar2_postgres():
    t, issues = type_mapping.to_postgres("VARCHAR2(100)")
    assert t == "VARCHAR(100)"


def test_date_postgres():
    t, issues = type_mapping.to_postgres("DATE")
    assert t == "TIMESTAMP"


def test_clob_postgres():
    t, issues = type_mapping.to_postgres("CLOB")
    assert t == "TEXT"


def test_blob_postgres():
    t, issues = type_mapping.to_postgres("BLOB")
    assert t == "BYTEA"


def test_rowid_postgres_flags_warning():
    t, issues = type_mapping.to_postgres("ROWID")
    assert t == "VARCHAR(18)"
    assert any(i.severity == "warning" for i in issues)


def test_timestamp_with_tz_postgres():
    t, issues = type_mapping.to_postgres("TIMESTAMP(6) WITH TIME ZONE")
    assert t == "TIMESTAMPTZ"


def test_sys_refcursor_maps_to_postgres_refcursor():
    """Real bug, reported from the field: SYS_REFCURSOR (Oracle's
    predefined weak ref cursor type, usable directly with no local `TYPE
    ... IS REF CURSOR` declaration) had no mapping rule at all and fell
    through to the generic fallback, silently becoming TEXT. A migrated
    routine then did `OPEN p_cursor FOR SELECT ...;` against a TEXT
    variable and failed at "Apply DDL to Target" with 'variable "p_cursor"
    must be of type cursor or refcursor' -- no issue was ever raised during
    conversion to explain why. PostgreSQL's own built-in REFCURSOR type is
    the direct, correct equivalent."""
    t, issues = type_mapping.to_postgres("SYS_REFCURSOR")
    assert t == "REFCURSOR"
    assert not any(i.severity == "error" for i in issues)


def test_number_integer_mysql():
    t, issues = type_mapping.to_mysql("NUMBER(7)")
    assert t == "INT"


def test_number_with_scale_mysql():
    t, issues = type_mapping.to_mysql("NUMBER(10,2)")
    assert t == "DECIMAL(10,2)"


def test_varchar2_mysql():
    t, issues = type_mapping.to_mysql("VARCHAR2(100)")
    assert t == "VARCHAR(100)"


def test_clob_mysql():
    t, issues = type_mapping.to_mysql("CLOB")
    assert t == "LONGTEXT"


def test_blob_mysql():
    t, issues = type_mapping.to_mysql("BLOB")
    assert t == "LONGBLOB"


def test_unknown_type_defaults_to_text_and_errors():
    t, issues = type_mapping.to_postgres("SOME_MADE_UP_TYPE")
    assert t == "TEXT"
    assert any(i.severity == "error" for i in issues)


def test_map_type_dispatch():
    t_pg, _ = type_mapping.map_type("NUMBER(3)", "PostgreSQL")
    t_my, _ = type_mapping.map_type("NUMBER(3)", "MySQL")
    assert t_pg == "SMALLINT"
    assert t_my == "SMALLINT"


def test_number_small_integer_sqlserver():
    t, issues = type_mapping.to_sqlserver("NUMBER(3)")
    assert t == "SMALLINT"


def test_number_integer_sqlserver():
    t, issues = type_mapping.to_sqlserver("NUMBER(7)")
    assert t == "INT"


def test_number_bigint_sqlserver():
    t, issues = type_mapping.to_sqlserver("NUMBER(15)")
    assert t == "BIGINT"


def test_number_with_scale_sqlserver():
    t, issues = type_mapping.to_sqlserver("NUMBER(10,2)")
    assert t == "DECIMAL(10,2)"


def test_number_no_precision_sqlserver_warns():
    t, issues = type_mapping.to_sqlserver("NUMBER")
    assert t == "DECIMAL(38,10)"
    assert any(i.severity == "warning" for i in issues)


def test_varchar2_sqlserver_sized():
    t, issues = type_mapping.to_sqlserver("VARCHAR2(100)")
    assert t == "VARCHAR(100)"


def test_varchar2_sqlserver_oversized_becomes_max():
    t, issues = type_mapping.to_sqlserver("VARCHAR2(8001)")
    assert t == "VARCHAR(MAX)"


def test_nvarchar2_stays_unicode_sqlserver():
    t, issues = type_mapping.to_sqlserver("NVARCHAR2(100)")
    assert t == "NVARCHAR(100)"


def test_date_sqlserver():
    t, issues = type_mapping.to_sqlserver("DATE")
    assert t == "DATETIME2"


def test_clob_sqlserver():
    t, issues = type_mapping.to_sqlserver("CLOB")
    assert t == "NVARCHAR(MAX)"


def test_blob_sqlserver():
    t, issues = type_mapping.to_sqlserver("BLOB")
    assert t == "VARBINARY(MAX)"


def test_boolean_sqlserver():
    t, issues = type_mapping.to_sqlserver("BOOLEAN")
    assert t == "BIT"


def test_xmltype_sqlserver():
    t, issues = type_mapping.to_sqlserver("XMLTYPE")
    assert t == "XML"


def test_interval_sqlserver_flags_error():
    t, issues = type_mapping.to_sqlserver("INTERVAL DAY TO SECOND")
    assert any(i.severity == "error" for i in issues)


def test_map_type_dispatch_sqlserver():
    t, _ = type_mapping.map_type("NUMBER(3)", "SQL Server")
    assert t == "SMALLINT"


def test_number_small_integer_db2():
    t, issues = type_mapping.to_db2("NUMBER(3)")
    assert t == "SMALLINT"


def test_number_integer_db2():
    t, issues = type_mapping.to_db2("NUMBER(7)")
    assert t == "INTEGER"


def test_number_bigint_db2():
    t, issues = type_mapping.to_db2("NUMBER(15)")
    assert t == "BIGINT"


def test_number_with_scale_db2():
    t, issues = type_mapping.to_db2("NUMBER(10,2)")
    assert t == "DECIMAL(10,2)"


def test_number_no_precision_db2_warns():
    t, issues = type_mapping.to_db2("NUMBER")
    assert t == "DECIMAL(31,9)"
    assert any(i.severity == "warning" for i in issues)


def test_number_exceeds_db2_max_precision_flags_info():
    t, issues = type_mapping.to_db2("NUMBER(35)")
    assert t == "DECIMAL(31,0)"
    assert any(i.severity == "info" for i in issues)


def test_varchar2_db2():
    t, issues = type_mapping.to_db2("VARCHAR2(100)")
    assert t == "VARCHAR(100)"


def test_date_db2():
    t, issues = type_mapping.to_db2("DATE")
    assert t == "TIMESTAMP"


def test_clob_db2_native():
    t, issues = type_mapping.to_db2("CLOB")
    assert t == "CLOB"


def test_blob_db2_native():
    t, issues = type_mapping.to_db2("BLOB")
    assert t == "BLOB"


def test_raw_sized_becomes_for_bit_data_db2():
    t, issues = type_mapping.to_db2("RAW(16)")
    assert t == "VARCHAR(16) FOR BIT DATA"


def test_raw_unsized_becomes_blob_db2():
    t, issues = type_mapping.to_db2("RAW")
    assert t == "BLOB"


def test_boolean_db2_native():
    t, issues = type_mapping.to_db2("BOOLEAN")
    assert t == "BOOLEAN"


def test_xmltype_db2_native():
    t, issues = type_mapping.to_db2("XMLTYPE")
    assert t == "XML"


def test_rowid_db2_flags_warning():
    t, issues = type_mapping.to_db2("ROWID")
    assert t == "VARCHAR(18)"
    assert any(i.severity == "warning" for i in issues)


def test_timestamp_with_tz_db2_flags_warning():
    t, issues = type_mapping.to_db2("TIMESTAMP(6) WITH TIME ZONE")
    assert t == "TIMESTAMP"
    assert any(i.severity == "warning" for i in issues)


def test_interval_db2_flags_error():
    t, issues = type_mapping.to_db2("INTERVAL DAY TO SECOND")
    assert any(i.severity == "error" for i in issues)


def test_unknown_type_db2_defaults_to_clob_and_errors():
    t, issues = type_mapping.to_db2("SOME_MADE_UP_TYPE")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_map_type_dispatch_db2():
    t, _ = type_mapping.map_type("NUMBER(3)", "DB2")
    assert t == "SMALLINT"


# -------------------------------------------------------------------- to_mongodb


def test_number_small_maps_to_int_mongodb():
    t, issues = type_mapping.to_mongodb("NUMBER(9)")
    assert t == "int"


def test_number_medium_maps_to_long_mongodb():
    t, issues = type_mapping.to_mongodb("NUMBER(15)")
    assert t == "long"


def test_number_large_maps_to_decimal_mongodb():
    t, issues = type_mapping.to_mongodb("NUMBER(35)")
    assert t == "decimal"
    assert any(i.severity == "info" for i in issues)


def test_number_with_scale_maps_to_decimal_mongodb():
    t, issues = type_mapping.to_mongodb("NUMBER(10,2)")
    assert t == "decimal"
    assert any(i.severity == "info" for i in issues)


def test_number_no_precision_mongodb_warns():
    t, issues = type_mapping.to_mongodb("NUMBER")
    assert t == "decimal"
    assert any(i.severity == "warning" for i in issues)


def test_varchar2_mongodb():
    t, issues = type_mapping.to_mongodb("VARCHAR2(100)")
    assert t == "string"


def test_date_mongodb():
    t, issues = type_mapping.to_mongodb("DATE")
    assert t == "date"


def test_timestamp_with_tz_mongodb_flags_info():
    t, issues = type_mapping.to_mongodb("TIMESTAMP(6) WITH TIME ZONE")
    assert t == "date"
    assert any(i.severity == "info" for i in issues)


def test_clob_mongodb_flags_size_limit_info():
    t, issues = type_mapping.to_mongodb("CLOB")
    assert t == "string"
    assert any(i.severity == "info" for i in issues)


def test_blob_mongodb_native():
    t, issues = type_mapping.to_mongodb("BLOB")
    assert t == "binData"


def test_boolean_mongodb_native():
    t, issues = type_mapping.to_mongodb("BOOLEAN")
    assert t == "bool"


def test_float_mongodb():
    t, issues = type_mapping.to_mongodb("BINARY_DOUBLE")
    assert t == "double"


def test_rowid_mongodb_flags_warning():
    t, issues = type_mapping.to_mongodb("ROWID")
    assert t == "string"
    assert any(i.severity == "warning" for i in issues)


def test_xmltype_mongodb_flags_info():
    t, issues = type_mapping.to_mongodb("XMLTYPE")
    assert t == "string"
    assert any(i.severity == "info" for i in issues)


def test_interval_mongodb_flags_error():
    t, issues = type_mapping.to_mongodb("INTERVAL DAY TO SECOND")
    assert t == "string"
    assert any(i.severity == "error" for i in issues)


def test_unknown_type_mongodb_defaults_to_string_and_errors():
    t, issues = type_mapping.to_mongodb("SOME_MADE_UP_TYPE")
    assert t == "string"
    assert any(i.severity == "error" for i in issues)


def test_map_type_dispatch_mongodb():
    t, _ = type_mapping.map_type("VARCHAR2(50)", "MongoDB")
    assert t == "string"


# ------------------------------------------------------- from_mysql (reverse)

def test_from_mysql_tinyint_1_is_boolean():
    t, issues = type_mapping.from_mysql("tinyint(1)")
    assert t == "BOOLEAN"
    assert issues == []


def test_from_mysql_plain_tinyint_is_small_number():
    t, issues = type_mapping.from_mysql("tinyint(4)")
    assert t == "NUMBER(3)"


def test_from_mysql_int_widths():
    assert type_mapping.from_mysql("smallint(5)")[0] == "NUMBER(5)"
    assert type_mapping.from_mysql("mediumint(7)")[0] == "NUMBER(7)"
    assert type_mapping.from_mysql("int(10)")[0] == "NUMBER(10)"
    assert type_mapping.from_mysql("bigint(19)")[0] == "NUMBER(19)"


def test_from_mysql_unsigned_flagged_and_stripped():
    t, issues = type_mapping.from_mysql("int(10) unsigned")
    assert t == "NUMBER(10)"
    assert any("UNSIGNED" in i.message for i in issues)


def test_from_mysql_decimal_with_precision_and_scale():
    t, issues = type_mapping.from_mysql("decimal(10,2)")
    assert t == "NUMBER(10,2)"
    assert issues == []


def test_from_mysql_decimal_no_scale_defaults_to_zero():
    t, _ = type_mapping.from_mysql("decimal(8)")
    assert t == "NUMBER(8,0)"


def test_from_mysql_float_and_double():
    assert type_mapping.from_mysql("float")[0] == "BINARY_FLOAT"
    assert type_mapping.from_mysql("double")[0] == "BINARY_DOUBLE"


def test_from_mysql_char_and_varchar():
    assert type_mapping.from_mysql("char(10)")[0] == "CHAR(10)"
    assert type_mapping.from_mysql("varchar(255)")[0] == "VARCHAR2(255)"
    assert type_mapping.from_mysql("varchar")[0] == "VARCHAR2(4000)"


def test_from_mysql_binary_and_varbinary():
    assert type_mapping.from_mysql("binary(16)")[0] == "RAW(16)"
    assert type_mapping.from_mysql("varbinary(255)")[0] == "RAW(255)"


def test_from_mysql_text_variants_map_to_clob():
    for variant in ("text", "tinytext", "mediumtext", "longtext"):
        assert type_mapping.from_mysql(variant)[0] == "CLOB"


def test_from_mysql_blob_variants_map_to_blob():
    for variant in ("blob", "tinyblob", "mediumblob", "longblob"):
        assert type_mapping.from_mysql(variant)[0] == "BLOB"


def test_from_mysql_date_flagged_info():
    t, issues = type_mapping.from_mysql("date")
    assert t == "DATE"
    assert any(i.severity == "info" for i in issues)


def test_from_mysql_datetime_and_timestamp_map_to_timestamp():
    assert type_mapping.from_mysql("datetime")[0] == "TIMESTAMP"
    assert type_mapping.from_mysql("timestamp")[0] == "TIMESTAMP"


def test_from_mysql_time_flagged_warning():
    t, issues = type_mapping.from_mysql("time")
    assert t == "VARCHAR2(20)"
    assert any(i.severity == "warning" for i in issues)


def test_from_mysql_year_is_small_number():
    assert type_mapping.from_mysql("year")[0] == "NUMBER(4)"


def test_from_mysql_bit_1_is_boolean():
    assert type_mapping.from_mysql("bit(1)")[0] == "BOOLEAN"


def test_from_mysql_bit_wide_maps_to_raw_bytes():
    t, issues = type_mapping.from_mysql("bit(9)")
    assert t == "RAW(2)"  # ceil(9/8)
    assert any(i.severity == "info" for i in issues)


def test_from_mysql_json_flagged_info():
    t, issues = type_mapping.from_mysql("json")
    assert t == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_mysql_spatial_type_flagged_error():
    t, issues = type_mapping.from_mysql("geometry")
    assert t == "BLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_mysql_enum_maps_to_varchar_sized_to_longest_literal():
    t, issues = type_mapping.from_mysql("enum('ACTIVE','INACTIVE')")
    assert t == "VARCHAR2(8)"  # len("INACTIVE") == 8
    assert any(i.severity == "warning" and "ENUM" in i.message for i in issues)


def test_from_mysql_set_maps_to_varchar():
    t, issues = type_mapping.from_mysql("set('a','b','c')")
    assert t.startswith("VARCHAR2(")
    assert any(i.severity == "warning" and "SET" in i.message for i in issues)


def test_from_mysql_unknown_type_defaults_to_clob_and_errors():
    t, issues = type_mapping.from_mysql("some_made_up_type")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_mysql_pivot_round_trips_through_existing_forward_mappers():
    # the whole point of the pivot design: a MySQL type, once reverse-mapped,
    # must be something to_postgres/to_mysql/to_sqlserver/to_db2 already
    # know how to handle without any changes on that side.
    pivot, _ = type_mapping.from_mysql("varchar(100)")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(100)"
    assert pg_issues == []


# --------------------------------------------------------------- from_postgres

def test_from_postgres_smallint_integer_bigint_widths():
    assert type_mapping.from_postgres("smallint")[0] == "NUMBER(5)"
    assert type_mapping.from_postgres("integer")[0] == "NUMBER(10)"
    assert type_mapping.from_postgres("bigint")[0] == "NUMBER(19)"


def test_from_postgres_numeric_with_precision_and_scale():
    assert type_mapping.from_postgres("numeric(10,2)")[0] == "NUMBER(10,2)"


def test_from_postgres_numeric_no_scale_defaults_to_zero():
    assert type_mapping.from_postgres("numeric(8)")[0] == "NUMBER(8,0)"


def test_from_postgres_unqualified_numeric_is_unbounded_pivot_number_no_issue():
    t, issues = type_mapping.from_postgres("numeric")
    assert t == "NUMBER"
    assert issues == []  # faithful representation, not lossy -- no issue raised here


def test_from_postgres_real_and_double_precision():
    assert type_mapping.from_postgres("real")[0] == "BINARY_FLOAT"
    assert type_mapping.from_postgres("double precision")[0] == "BINARY_DOUBLE"


def test_from_postgres_char_and_varchar_sized():
    assert type_mapping.from_postgres("character(10)")[0] == "CHAR(10)"
    assert type_mapping.from_postgres("character varying(255)")[0] == "VARCHAR2(255)"


def test_from_postgres_unbounded_varchar_maps_to_clob_with_info():
    t, issues = type_mapping.from_postgres("character varying")
    assert t == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_oversized_varchar_maps_to_clob_with_info():
    t, issues = type_mapping.from_postgres("character varying(8000)")
    assert t == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_text_and_bytea():
    assert type_mapping.from_postgres("text")[0] == "CLOB"
    assert type_mapping.from_postgres("bytea")[0] == "BLOB"


def test_from_postgres_boolean():
    assert type_mapping.from_postgres("boolean")[0] == "BOOLEAN"


def test_from_postgres_date_flagged_info():
    t, issues = type_mapping.from_postgres("date")
    assert t == "DATE"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_timestamp_variants():
    assert type_mapping.from_postgres("timestamp without time zone")[0] == "TIMESTAMP"
    assert type_mapping.from_postgres("timestamp with time zone")[0] == "TIMESTAMP WITH TIME ZONE"


def test_from_postgres_time_flagged_warning():
    t, issues = type_mapping.from_postgres("time without time zone")
    assert t == "VARCHAR2(20)"
    assert any(i.severity == "warning" for i in issues)


def test_from_postgres_timetz_flagged_warning():
    t, issues = type_mapping.from_postgres("time with time zone")
    assert t == "VARCHAR2(30)"
    assert any(i.severity == "warning" for i in issues)


def test_from_postgres_interval_year_to_month():
    assert type_mapping.from_postgres("interval year to month")[0] == "INTERVAL YEAR TO MONTH"


def test_from_postgres_interval_day_to_second():
    assert type_mapping.from_postgres("interval day to second")[0] == "INTERVAL DAY TO SECOND"


def test_from_postgres_generic_interval_flagged_info():
    t, issues = type_mapping.from_postgres("interval")
    assert t == "INTERVAL DAY TO SECOND"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_uuid_flagged_warning():
    t, issues = type_mapping.from_postgres("uuid")
    assert t == "VARCHAR2(36)"
    assert any(i.severity == "warning" for i in issues)


def test_from_postgres_json_and_jsonb_flagged_info():
    t1, issues1 = type_mapping.from_postgres("json")
    t2, issues2 = type_mapping.from_postgres("jsonb")
    assert t1 == "CLOB" and t2 == "CLOB"
    assert any(i.severity == "info" for i in issues1)
    assert any(i.severity == "info" for i in issues2)


def test_from_postgres_xml():
    assert type_mapping.from_postgres("xml")[0] == "XMLTYPE"


def test_from_postgres_money_flagged_info():
    t, issues = type_mapping.from_postgres("money")
    assert t == "NUMBER(19,4)"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_network_types_flagged_warning():
    for raw in ("inet", "cidr", "macaddr", "macaddr8"):
        t, issues = type_mapping.from_postgres(raw)
        assert t == "VARCHAR2(43)"
        assert any(i.severity == "warning" for i in issues)


def test_from_postgres_bit_1_is_boolean():
    assert type_mapping.from_postgres("bit(1)")[0] == "BOOLEAN"


def test_from_postgres_bit_wide_maps_to_raw_bytes():
    t, issues = type_mapping.from_postgres("bit(9)")
    assert t == "RAW(2)"  # ceil(9/8)
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_bit_varying_maps_to_raw_bytes():
    t, issues = type_mapping.from_postgres("bit varying(16)")
    assert t == "RAW(2)"
    assert any(i.severity == "info" for i in issues)


def test_from_postgres_array_type_flagged_error():
    t, issues = type_mapping.from_postgres("integer[]")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_postgres_unknown_type_defaults_to_clob_and_errors():
    t, issues = type_mapping.from_postgres("some_enum_type")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_postgres_pivot_round_trips_through_existing_forward_mappers():
    # same round-trip guarantee from_mysql already has -- a PostgreSQL type,
    # once reverse-mapped, must be something to_postgres/to_mysql/
    # to_sqlserver/to_db2 already know how to handle with no changes.
    pivot, _ = type_mapping.from_postgres("character varying(100)")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(100)"
    assert pg_issues == []


# -------------------------------------------------------------- from_sqlserver

def test_from_sqlserver_int_widths():
    assert type_mapping.from_sqlserver("tinyint")[0] == "NUMBER(3)"
    assert type_mapping.from_sqlserver("smallint")[0] == "NUMBER(5)"
    assert type_mapping.from_sqlserver("int")[0] == "NUMBER(10)"
    assert type_mapping.from_sqlserver("bigint")[0] == "NUMBER(19)"


def test_from_sqlserver_decimal_with_precision_and_scale():
    assert type_mapping.from_sqlserver("decimal(10,2)")[0] == "NUMBER(10,2)"


def test_from_sqlserver_decimal_no_scale_defaults_to_zero():
    assert type_mapping.from_sqlserver("numeric(8)")[0] == "NUMBER(8,0)"


def test_from_sqlserver_money_types():
    assert type_mapping.from_sqlserver("money")[0] == "NUMBER(19,4)"
    assert type_mapping.from_sqlserver("smallmoney")[0] == "NUMBER(10,4)"


def test_from_sqlserver_bit_is_boolean():
    assert type_mapping.from_sqlserver("bit")[0] == "BOOLEAN"


def test_from_sqlserver_real_and_float():
    assert type_mapping.from_sqlserver("real")[0] == "BINARY_FLOAT"
    assert type_mapping.from_sqlserver("float")[0] == "BINARY_DOUBLE"


def test_from_sqlserver_char_and_varchar_sized():
    assert type_mapping.from_sqlserver("char(10)")[0] == "CHAR(10)"
    assert type_mapping.from_sqlserver("varchar(255)")[0] == "VARCHAR2(255)"
    assert type_mapping.from_sqlserver("nchar(5)")[0] == "CHAR(5)"
    assert type_mapping.from_sqlserver("nvarchar(100)")[0] == "VARCHAR2(100)"


def test_from_sqlserver_varchar_max_maps_to_clob():
    assert type_mapping.from_sqlserver("varchar(max)")[0] == "CLOB"
    assert type_mapping.from_sqlserver("nvarchar(max)")[0] == "CLOB"


def test_from_sqlserver_text_and_ntext():
    assert type_mapping.from_sqlserver("text")[0] == "CLOB"
    assert type_mapping.from_sqlserver("ntext")[0] == "CLOB"


def test_from_sqlserver_binary_and_varbinary():
    assert type_mapping.from_sqlserver("binary(16)")[0] == "RAW(16)"
    assert type_mapping.from_sqlserver("varbinary(50)")[0] == "RAW(50)"
    assert type_mapping.from_sqlserver("varbinary(max)")[0] == "BLOB"
    assert type_mapping.from_sqlserver("image")[0] == "BLOB"


def test_from_sqlserver_date_flagged_info():
    t, issues = type_mapping.from_sqlserver("date")
    assert t == "DATE"
    assert any(i.severity == "info" for i in issues)


def test_from_sqlserver_datetime_variants():
    assert type_mapping.from_sqlserver("datetime")[0] == "TIMESTAMP"
    assert type_mapping.from_sqlserver("datetime2(7)")[0] == "TIMESTAMP"
    assert type_mapping.from_sqlserver("smalldatetime")[0] == "TIMESTAMP"
    assert type_mapping.from_sqlserver("datetimeoffset")[0] == "TIMESTAMP WITH TIME ZONE"


def test_from_sqlserver_time_flagged_warning():
    t, issues = type_mapping.from_sqlserver("time(7)")
    assert t == "VARCHAR2(20)"
    assert any(i.severity == "warning" for i in issues)


def test_from_sqlserver_uniqueidentifier_flagged_warning():
    t, issues = type_mapping.from_sqlserver("uniqueidentifier")
    assert t == "VARCHAR2(36)"
    assert any(i.severity == "warning" for i in issues)


def test_from_sqlserver_xml():
    assert type_mapping.from_sqlserver("xml")[0] == "XMLTYPE"


def test_from_sqlserver_rowversion_flagged_warning():
    t, issues = type_mapping.from_sqlserver("rowversion")
    assert t == "RAW(8)"
    assert any(i.severity == "warning" for i in issues)
    t2, issues2 = type_mapping.from_sqlserver("timestamp")
    assert t2 == "RAW(8)"
    assert any(i.severity == "warning" for i in issues2)


def test_from_sqlserver_sql_variant_flagged_error():
    t, issues = type_mapping.from_sqlserver("sql_variant")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_sqlserver_hierarchyid_flagged_error():
    t, issues = type_mapping.from_sqlserver("hierarchyid")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_sqlserver_spatial_types_flagged_error():
    t, issues = type_mapping.from_sqlserver("geometry")
    assert t == "BLOB"
    assert any(i.severity == "error" for i in issues)
    t2, issues2 = type_mapping.from_sqlserver("geography")
    assert t2 == "BLOB"
    assert any(i.severity == "error" for i in issues2)


def test_from_sqlserver_unknown_type_defaults_to_clob_and_errors():
    t, issues = type_mapping.from_sqlserver("some_made_up_type")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_from_sqlserver_pivot_round_trips_through_existing_forward_mappers():
    pivot, _ = type_mapping.from_sqlserver("varchar(100)")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(100)"
    assert pg_issues == []


# -------------------------------------------------------------------- from_db2

def test_from_db2_int_widths():
    assert type_mapping.from_db2("smallint")[0] == "NUMBER(5)"
    assert type_mapping.from_db2("integer")[0] == "NUMBER(10)"
    assert type_mapping.from_db2("bigint")[0] == "NUMBER(19)"


def test_from_db2_decimal_with_precision_and_scale():
    assert type_mapping.from_db2("decimal(10,2)")[0] == "NUMBER(10,2)"


def test_from_db2_decimal_no_scale_defaults_to_zero():
    assert type_mapping.from_db2("decimal(8)")[0] == "NUMBER(8,0)"


def test_from_db2_decfloat_flagged_info():
    t, issues = type_mapping.from_db2("decfloat(16)")
    assert t == "NUMBER"
    assert any(i.severity == "info" for i in issues)


def test_from_db2_real_and_double():
    assert type_mapping.from_db2("real")[0] == "BINARY_FLOAT"
    assert type_mapping.from_db2("double")[0] == "BINARY_DOUBLE"


def test_from_db2_char_and_varchar_sized():
    assert type_mapping.from_db2("char(10)")[0] == "CHAR(10)"
    assert type_mapping.from_db2("varchar(255)")[0] == "VARCHAR2(255)"


def test_from_db2_clob():
    assert type_mapping.from_db2("clob")[0] == "CLOB"


def test_from_db2_graphic_types_flagged_info():
    t1, issues1 = type_mapping.from_db2("graphic(10)")
    assert t1 == "CHAR(10)"
    assert any(i.severity == "info" for i in issues1)
    t2, issues2 = type_mapping.from_db2("vargraphic(50)")
    assert t2 == "VARCHAR2(50)"
    assert any(i.severity == "info" for i in issues2)
    t3, issues3 = type_mapping.from_db2("dbclob")
    assert t3 == "VARCHAR2(4000)"
    assert any(i.severity == "info" for i in issues3)


def test_from_db2_binary_types():
    assert type_mapping.from_db2("binary(16)")[0] == "RAW(16)"
    assert type_mapping.from_db2("varbinary(50)")[0] == "RAW(50)"
    assert type_mapping.from_db2("blob")[0] == "BLOB"


def test_from_db2_date_flagged_info():
    t, issues = type_mapping.from_db2("date")
    assert t == "DATE"
    assert any(i.severity == "info" for i in issues)


def test_from_db2_timestamp():
    assert type_mapping.from_db2("timestamp")[0] == "TIMESTAMP"


def test_from_db2_time_flagged_warning():
    t, issues = type_mapping.from_db2("time")
    assert t == "VARCHAR2(20)"
    assert any(i.severity == "warning" for i in issues)


def test_from_db2_boolean():
    assert type_mapping.from_db2("boolean")[0] == "BOOLEAN"


def test_from_db2_xml():
    assert type_mapping.from_db2("xml")[0] == "XMLTYPE"


def test_from_db2_rowid_maps_to_pivot_rowid():
    t, issues = type_mapping.from_db2("rowid")
    assert t == "ROWID"
    assert issues == []  # no source-side issue -- the existing to_* ROWID warnings already cover it


def test_from_db2_rowid_round_trips_with_existing_forward_warning():
    pivot, _ = type_mapping.from_db2("rowid")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(18)"
    assert any(i.severity == "warning" for i in pg_issues)


def test_from_db2_unknown_type_defaults_to_clob_and_errors():
    t, issues = type_mapping.from_db2("some_made_up_type")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


# ------------------------------------------------------------- from_mongodb


def test_from_mongodb_string_sized_from_max_length():
    t, issues = type_mapping.from_mongodb("string", max_length=10)
    assert t.startswith("VARCHAR2(")
    assert issues == []


def test_from_mongodb_string_no_length_falls_back_to_clob():
    t, issues = type_mapping.from_mongodb("string", max_length=None)
    assert t == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_mongodb_string_long_falls_back_to_clob():
    t, issues = type_mapping.from_mongodb("string", max_length=5000)
    assert t == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_mongodb_int_and_long():
    assert type_mapping.from_mongodb("int")[0] == "NUMBER(9)"
    assert type_mapping.from_mongodb("long")[0] == "NUMBER(19)"


def test_from_mongodb_double():
    assert type_mapping.from_mongodb("double")[0] == "BINARY_DOUBLE"


def test_from_mongodb_decimal_flagged_info():
    t, issues = type_mapping.from_mongodb("decimal")
    assert t == "NUMBER(38,10)"
    assert any(i.severity == "info" for i in issues)


def test_from_mongodb_bool():
    assert type_mapping.from_mongodb("bool")[0] == "BOOLEAN"


def test_from_mongodb_date():
    assert type_mapping.from_mongodb("date")[0] == "TIMESTAMP"


def test_from_mongodb_objectid_maps_to_pivot_rowid():
    t, issues = type_mapping.from_mongodb("objectId")
    assert t == "ROWID"
    assert issues == []  # existing to_* ROWID warnings already cover it


def test_from_mongodb_objectid_round_trips_with_existing_forward_warning():
    pivot, _ = type_mapping.from_mongodb("objectId")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(18)"
    assert any(i.severity == "warning" for i in pg_issues)


def test_from_mongodb_bindata():
    assert type_mapping.from_mongodb("binData")[0] == "BLOB"


def test_from_mongodb_unknown_type_defaults_to_varchar_and_warns():
    t, issues = type_mapping.from_mongodb("unknown")
    assert t == "VARCHAR2(4000)"
    assert any(i.severity == "warning" for i in issues)


# --------------------------------------------------------------- to_oracle


def test_to_oracle_number_passes_through_unchanged():
    assert type_mapping.to_oracle("NUMBER(10,2)")[0] == "NUMBER(10,2)"
    assert type_mapping.to_oracle("NUMBER(9)")[0] == "NUMBER(9)"
    assert type_mapping.to_oracle("NUMBER")[0] == "NUMBER"


def test_to_oracle_number_no_issues():
    assert type_mapping.to_oracle("NUMBER(10,2)")[1] == []


def test_to_oracle_varchar2_passes_through_unchanged():
    assert type_mapping.to_oracle("VARCHAR2(100)")[0] == "VARCHAR2(100)"


def test_to_oracle_clob_blob_passes_through_unchanged():
    assert type_mapping.to_oracle("CLOB")[0] == "CLOB"
    assert type_mapping.to_oracle("BLOB")[0] == "BLOB"


def test_to_oracle_timestamp_with_time_zone_passes_through_unchanged():
    t, issues = type_mapping.to_oracle("TIMESTAMP(6) WITH TIME ZONE")
    assert t == "TIMESTAMP(6) WITH TIME ZONE"
    assert issues == []


def test_to_oracle_rowid_passes_through_with_no_issue():
    # Unlike every other to_*() mapper, Oracle actually *has* ROWID
    # natively -- this must not get the "no equivalent" warning the other
    # targets attach.
    t, issues = type_mapping.to_oracle("ROWID")
    assert t == "ROWID"
    assert issues == []


def test_to_oracle_boolean_flagged_info_about_23c():
    t, issues = type_mapping.to_oracle("BOOLEAN")
    assert t == "BOOLEAN"
    assert any(i.severity == "info" and "23c" in i.message for i in issues)


def test_to_oracle_interval_types_pass_through_unchanged():
    assert type_mapping.to_oracle("INTERVAL YEAR TO MONTH")[0] == "INTERVAL YEAR TO MONTH"
    assert type_mapping.to_oracle("INTERVAL DAY TO SECOND")[0] == "INTERVAL DAY TO SECOND"


def test_to_oracle_sys_xmltype_collapses_to_xmltype():
    assert type_mapping.to_oracle("SYS.XMLTYPE")[0] == "XMLTYPE"


def test_to_oracle_raw_passes_through_with_size():
    assert type_mapping.to_oracle("RAW(2000)")[0] == "RAW(2000)"


def test_to_oracle_unknown_type_defaults_to_clob_and_errors():
    t, issues = type_mapping.to_oracle("some_made_up_type")
    assert t == "CLOB"
    assert any(i.severity == "error" for i in issues)


def test_to_oracle_case_and_whitespace_normalized():
    assert type_mapping.to_oracle("varchar2(50)")[0] == "VARCHAR2(50)"
    assert type_mapping.to_oracle("  CLOB  ")[0] == "CLOB"
    assert type_mapping.to_oracle("timestamp(6)  with   time zone")[0] == "TIMESTAMP(6) WITH TIME ZONE"


def test_to_oracle_round_trips_every_from_engine_mapping():
    # Every non-Mongo, non-Oracle from_<engine>() reverse mapper should
    # produce a pivot type to_oracle() recognizes without erroring --
    # this is the whole point of the shared pivot representation.
    from_results = [
        type_mapping.from_mysql("varchar(100)")[0],
        type_mapping.from_mysql("int(10)")[0],
        type_mapping.from_postgres("numeric(10,2)")[0],
        type_mapping.from_sqlserver("nvarchar(50)")[0],
        type_mapping.from_db2("varchar(255)")[0],
        type_mapping.from_mongodb("string", max_length=10)[0],
        type_mapping.from_mongodb("objectId")[0],
    ]
    for pivot_type in from_results:
        _t, issues = type_mapping.to_oracle(pivot_type)
        assert not any(i.severity == "error" for i in issues), f"{pivot_type} -> unexpected error"


def test_from_db2_pivot_round_trips_through_existing_forward_mappers():
    pivot, _ = type_mapping.from_db2("varchar(100)")
    pg_type, pg_issues = type_mapping.to_postgres(pivot)
    assert pg_type == "VARCHAR(100)"
    assert pg_issues == []


# ------------------------------------------------------------ from_spreadsheet
# Reverse-mapping inferred Excel/CSV cell types into the shared pivot
# representation, so the existing forward mappers convert a
# spreadsheet-sourced column to any target unchanged.


def test_from_spreadsheet_integers_land_on_native_integer_precisions():
    # NUMBER(9)/NUMBER(18) map to a real INTEGER/BIGINT on every target;
    # NUMBER(19) silently degrades to DECIMAL/NUMERIC everywhere.
    assert type_mapping.from_spreadsheet("int", 1)[0] == "NUMBER(9)"
    assert type_mapping.from_spreadsheet("int", 9)[0] == "NUMBER(9)"
    assert type_mapping.from_spreadsheet("int", 10)[0] == "NUMBER(18)"
    assert type_mapping.from_spreadsheet("int", 18)[0] == "NUMBER(18)"


def test_from_spreadsheet_integer_precisions_survive_every_forward_mapper():
    for pivot in ("NUMBER(9)", "NUMBER(18)"):
        for engine in ("PostgreSQL", "MySQL", "SQL Server", "DB2"):
            _, issues = type_mapping.map_type(pivot, engine)
            assert issues == [], f"{pivot} -> {engine} should be a clean native integer"


def test_from_spreadsheet_oversized_integer_is_flagged():
    pivot, issues = type_mapping.from_spreadsheet("int", 25)
    assert pivot == "NUMBER(38)"
    assert any("64-bit" in i.message for i in issues)


def test_from_spreadsheet_int_without_a_width_defaults_to_bigint():
    assert type_mapping.from_spreadsheet("int")[0] == "NUMBER(18)"


def test_from_spreadsheet_text_is_padded_above_the_sampled_maximum():
    # Only a sample of the file was read, so the observed maximum is a
    # floor, not a ceiling.
    pivot, issues = type_mapping.from_spreadsheet("string", 10)
    assert pivot.startswith("VARCHAR2(")
    assert int(pivot[len("VARCHAR2("):-1]) > 10
    assert issues == []


def test_from_spreadsheet_long_text_becomes_clob():
    pivot, issues = type_mapping.from_spreadsheet("string", 9000)
    assert pivot == "CLOB"
    assert any(i.severity == "info" for i in issues)


def test_from_spreadsheet_text_of_unknown_length_becomes_clob():
    assert type_mapping.from_spreadsheet("string", None)[0] == "CLOB"


def test_from_spreadsheet_scalar_types():
    assert type_mapping.from_spreadsheet("float")[0] == "BINARY_DOUBLE"
    assert type_mapping.from_spreadsheet("bool")[0] == "BOOLEAN"
    assert type_mapping.from_spreadsheet("date")[0] == "DATE"
    assert type_mapping.from_spreadsheet("datetime")[0] == "TIMESTAMP"
    assert type_mapping.from_spreadsheet("binary")[0] == "BLOB"


def test_from_spreadsheet_decimal_is_flagged_as_a_guess():
    pivot, issues = type_mapping.from_spreadsheet("decimal")
    assert pivot == "NUMBER(38,10)"
    assert any(i.severity == "info" for i in issues)


def test_from_spreadsheet_time_falls_back_to_text_with_an_explanation():
    pivot, issues = type_mapping.from_spreadsheet("time")
    assert pivot == "VARCHAR2(8)"
    assert any("TIME" in i.message for i in issues)


def test_from_spreadsheet_unobserved_column_warns():
    pivot, issues = type_mapping.from_spreadsheet("null")
    assert pivot == "VARCHAR2(4000)"
    assert any(i.severity == "warning" for i in issues)


def test_from_spreadsheet_unrecognized_type_warns_rather_than_raising():
    pivot, issues = type_mapping.from_spreadsheet("wat")
    assert pivot == "VARCHAR2(4000)"
    assert any(i.severity == "warning" for i in issues)


# ---------------------------------------------------------------------
# MySQL fractional-seconds precision.
#
# MySQL's bare DATETIME stores whole seconds only -- fractional digits
# are opt-in as DATETIME(n), n in 0..6 -- while the Oracle-flavoured
# pivot's bare TIMESTAMP means TIMESTAMP(6). Mapping one to the other
# discarded every microsecond on write, and reported it to the user only
# as a checksum mismatch, i.e. as "Unvalidated". Found by the integration
# harness against a real MariaDB; nothing in this file covered the MySQL
# TIMESTAMP path at all before, which is how it survived.
#
# MySQL is alone in this: PostgreSQL's TIMESTAMP and Db2's are both 6
# digits by default and SQL Server's DATETIME2 is 7, so all three are
# asserted here too as a guard against the same mistake being made in
# another mapper later.

def test_mysql_timestamp_keeps_fractional_seconds():
    t, issues = type_mapping.to_mysql("TIMESTAMP")
    assert t == "DATETIME(6)", "a bare pivot TIMESTAMP means TIMESTAMP(6)"


def test_mysql_timestamp_honours_an_explicit_precision():
    assert type_mapping.to_mysql("TIMESTAMP(3)")[0] == "DATETIME(3)"
    assert type_mapping.to_mysql("TIMESTAMP(0)")[0] == "DATETIME"


def test_mysql_timestamp_precision_is_clamped_to_what_mysql_accepts():
    """DATETIME(9) is a syntax error. Rounding down to the most MySQL can
    store is both what the user wanted and the only thing that runs."""
    assert type_mapping.to_mysql("TIMESTAMP(9)")[0] == "DATETIME(6)"


def test_mysql_timestamp_with_time_zone_still_warns_and_keeps_precision():
    t, issues = type_mapping.to_mysql("TIMESTAMP(6) WITH TIME ZONE")
    assert t == "DATETIME(6)"
    assert any("time zone" in i.message.lower() for i in issues)


def test_the_other_targets_already_default_to_full_precision():
    assert type_mapping.to_postgres("TIMESTAMP")[0] == "TIMESTAMP"     # 6 digits
    assert type_mapping.to_db2("TIMESTAMP")[0] == "TIMESTAMP"          # 6 digits
    assert type_mapping.to_sqlserver("TIMESTAMP")[0] == "DATETIME2"    # 7 digits
