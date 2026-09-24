from tgdatabridge.utils.sql_split import split_sql_statements


def test_splits_simple_statements():
    text = 'CREATE TABLE "A" (x INT);\nCREATE TABLE "B" (y INT);'
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert stmts[0].strip().startswith('CREATE TABLE "A"')
    assert stmts[1].strip().startswith('CREATE TABLE "B"')


def test_does_not_split_inside_dollar_quoted_function_body():
    text = (
        'CREATE OR REPLACE FUNCTION "F"() RETURNS TRIGGER AS $$\n'
        "BEGIN\n"
        "  NEW.x := 1;\n"
        "  NEW.y := 2;\n"
        "  RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        'CREATE TRIGGER "T" BEFORE INSERT ON "A" FOR EACH ROW EXECUTE FUNCTION "F"();'
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "NEW.x := 1;" in stmts[0]
    assert "NEW.y := 2;" in stmts[0]
    assert "$$ LANGUAGE plpgsql" in stmts[0]
    assert stmts[1].strip().startswith('CREATE TRIGGER "T"')


def test_does_not_split_inside_named_dollar_tag():
    text = 'CREATE FUNCTION "F"() RETURNS INT AS $body$\nBEGIN\n  RETURN 1;\nEND;\n$body$ LANGUAGE plpgsql;'
    stmts = split_sql_statements(text)
    assert len(stmts) == 1
    assert "RETURN 1;" in stmts[0]


def test_does_not_split_inside_string_literal_semicolons():
    text = "INSERT INTO \"T\" (msg) VALUES ('hello; world');\nSELECT 1;"
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "hello; world" in stmts[0]


def test_pure_comment_headers_do_not_produce_empty_statements():
    text = (
        "-- Sequences (0)\n\n\n"
        "-- Tables (1)\n"
        'CREATE TABLE "A" (x INT);\n\n'
        "-- Views (0)\n\n\n"
        "-- Routines / Triggers (0)"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 1
    assert 'CREATE TABLE "A"' in stmts[0]


def test_empty_input_yields_no_statements():
    assert split_sql_statements("") == []
    assert split_sql_statements("   \n\n  ") == []
    assert split_sql_statements("-- just a comment") == []


# --------------------------------------------------- T-SQL BEGIN...END awareness


def test_does_not_split_inside_tsql_procedure_body():
    # T-SQL has no dollar-quoting -- a CREATE OR ALTER PROCEDURE body is a
    # plain BEGIN...END block full of internal ';'-terminated statements
    # that must not be treated as separate top-level statements.
    text = (
        "CREATE OR ALTER PROCEDURE [P1]\n"
        "AS\n"
        "BEGIN\n"
        "  SET NOCOUNT ON;\n"
        "  DECLARE @x INT = 1;\n"
        "  SET @x = @x + 1;\n"
        "END;\n"
        "CREATE OR ALTER PROCEDURE [P2]\n"
        "AS\n"
        "BEGIN\n"
        "  SET NOCOUNT ON;\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "SET @x = @x + 1;" in stmts[0]
    assert stmts[1].strip().startswith("CREATE OR ALTER PROCEDURE [P2]")


def test_does_not_split_inside_tsql_try_catch():
    text = (
        "CREATE OR ALTER PROCEDURE [P1]\n"
        "AS\n"
        "BEGIN\n"
        "  BEGIN TRY\n"
        "    SET @x = 1;\n"
        "  END TRY\n"
        "  BEGIN CATCH\n"
        "    THROW;\n"
        "  END CATCH\n"
        "END;\n"
        "SELECT 1;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "BEGIN CATCH" in stmts[0] and "END CATCH" in stmts[0]
    assert stmts[1].strip() == "SELECT 1"


def test_does_not_split_inside_tsql_idempotency_guard():
    # Tables/sequences/FKs for a SQL Server target are wrapped in
    # IF ... BEGIN ... END; idempotency guards (see ddl_generator).
    text = (
        "IF OBJECT_ID(N'[EMPLOYEES]', N'U') IS NULL\n"
        "BEGIN\n"
        "  CREATE TABLE [EMPLOYEES] (\n"
        "  [EMP_ID] INT NOT NULL,\n"
        "  [NAME] VARCHAR(100) NOT NULL\n"
        "  );\n"
        "END;\n"
        "IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE name = 'S1')\n"
        "BEGIN\n"
        "  CREATE SEQUENCE [S1] AS BIGINT START WITH 1 INCREMENT BY 1 NO CYCLE;\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "[EMP_ID] INT NOT NULL" in stmts[0]
    assert "CREATE SEQUENCE" in stmts[1]


def test_tsql_numeric_range_while_loop_semicolons_not_split():
    text = (
        "CREATE OR ALTER PROCEDURE [P1]\n"
        "AS\n"
        "BEGIN\n"
        "  DECLARE @I INT = 1;\n"
        "  WHILE @I <= 5\n"
        "  BEGIN\n"
        "    SET @I = @I + 1;\n"
        "  END\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 1
    assert "SET @I = @I + 1;" in stmts[0]


# --------------------------------------------------------- Db2 SQL PL awareness


def test_does_not_split_inside_db2_procedure_with_if_and_end_if():
    # Db2's IF/END IF, unlike T-SQL, is a two-word closer of its own -- a
    # bare-END-only scan would wrongly treat the "END" in "END IF" as
    # closing the outer BEGIN one statement early.
    text = (
        'CREATE OR REPLACE PROCEDURE "P1"()\n'
        "LANGUAGE SQL\n"
        "BEGIN\n"
        "  DECLARE X INT DEFAULT 1;\n"
        "  IF X = 1 THEN\n"
        "    SET X = 2;\n"
        "  END IF;\n"
        "  SET X = X + 1;\n"
        "END;\n"
        'CREATE OR REPLACE PROCEDURE "P2"()\n'
        "LANGUAGE SQL\n"
        "BEGIN\n"
        "  SET X = 1;\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "SET X = X + 1;" in stmts[0]
    assert stmts[1].strip().startswith('CREATE OR REPLACE PROCEDURE "P2"')


def test_does_not_split_inside_db2_row_loop_end_for():
    # A converted Db2 row/cursor FOR loop closes with "END FOR label;" --
    # this must not be mistaken for the routine's own closing "END;".
    text = (
        'CREATE OR REPLACE PROCEDURE "P1"()\n'
        "LANGUAGE SQL\n"
        "BEGIN\n"
        '  DECLARE C CURSOR FOR\n'
        "    SELECT ID FROM T;\n"
        "  LBL_REC_1: FOR REC AS C CURSOR FOR\n"
        "    SELECT ID FROM T\n"
        "  DO\n"
        "    SET X = X + REC.ID;\n"
        "  END FOR LBL_REC_1;\n"
        "  SET X = X + 1;\n"
        "END;\n"
        "SELECT 1 FROM SYSIBM.SYSDUMMY1;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "END FOR LBL_REC_1;" in stmts[0]
    assert "SET X = X + 1;" in stmts[0]
    assert stmts[1].strip() == "SELECT 1 FROM SYSIBM.SYSDUMMY1"


def test_does_not_split_inside_db2_idempotency_guard():
    text = (
        "BEGIN\n"
        "  IF NOT EXISTS (SELECT 1 FROM SYSCAT.TABLES WHERE TABNAME = 'T1') THEN\n"
        "    EXECUTE IMMEDIATE 'CREATE TABLE \"T1\" (X INT)';\n"
        "  END IF;\n"
        "END;\n"
        "BEGIN\n"
        "  IF NOT EXISTS (SELECT 1 FROM SYSCAT.SEQUENCES WHERE SEQNAME = 'S1') THEN\n"
        "    EXECUTE IMMEDIATE 'CREATE SEQUENCE \"S1\" AS BIGINT START WITH 1 INCREMENT BY 1 NO CYCLE';\n"
        "  END IF;\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert "CREATE TABLE" in stmts[0]
    assert "CREATE SEQUENCE" in stmts[1]


# --------------------------------------------------- comment awareness


def test_apostrophe_in_line_comment_does_not_desync_quote_state():
    # A real bug: an English contraction apostrophe (e.g. "tool's") inside
    # a "--" comment used to be misread as opening a genuine SQL string
    # literal, silently swallowing every ';' after it (including real
    # statement terminators) until some unrelated, later apostrophe
    # happened to close it back out again.
    text = (
        "-- Note: this tool's converters are Oracle-specific.\n"
        "CREATE TABLE \"A\" (x INT);\n"
        'CREATE TABLE "B" (y INT);'
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert 'CREATE TABLE "A"' in stmts[0]
    assert stmts[1].strip().startswith('CREATE TABLE "B"')


def test_block_comment_wrapping_begin_end_is_not_split():
    # A MANUAL-CONVERSION placeholder (emitted for non-Oracle-sourced
    # routines, and unconditionally for MongoDB-target routines/views)
    # wraps the routine's original source -- often a real BEGIN...END
    # block with its own internal ';' -- in /* ... */, with ddl_generator
    # appending its own forced "\n;" terminator (on its own line, after
    # the closing "*/") so the statement splitter reliably flushes it.
    # None of the BEGIN...END's internal ';'s may be treated as live,
    # splittable SQL, and no dangling '*/'-only fragment may appear.
    block = (
        "-- MANUAL CONVERSION REQUIRED for PROCEDURE RAISE_SALARY\n"
        "/*\nPROCEDURE RAISE_SALARY IS BEGIN UPDATE T SET X = 1; END;\n*/"
    )
    text = block + "\n;\n\n" + 'CREATE TABLE "A" (x INT);'
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert not any(s.strip() == "*/" for s in stmts)
    assert "BEGIN UPDATE T SET X = 1; END;" in stmts[0]
    assert stmts[1].strip().startswith('CREATE TABLE "A"')


def test_block_comment_with_semicolons_between_two_real_statements():
    # The comment's own internal ';'s (from "BEGIN DROP TABLE A; END;")
    # must not fragment the statement that follows it -- the whole
    # comment is inert leading text glued onto the next real statement
    # (same as any other non-terminated prefix), not a split point.
    text = (
        'CREATE TABLE "A" (x INT);\n'
        "/* old definition: BEGIN DROP TABLE A; END; */\n"
        'CREATE TABLE "B" (y INT);'
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert stmts[0].strip().startswith('CREATE TABLE "A"')
    assert 'CREATE TABLE "B"' in stmts[1]


def test_unterminated_block_comment_consumes_rest_of_text():
    text = "CREATE TABLE \"A\" (x INT); /* never closes; more; text"
    stmts = split_sql_statements(text)
    assert len(stmts) == 2
    assert stmts[1].strip().startswith("/* never closes")


def test_db2_while_and_loop_with_leave_iterate_not_split():
    text = (
        'CREATE OR REPLACE PROCEDURE "P1"()\n'
        "LANGUAGE SQL\n"
        "BEGIN\n"
        "  DECLARE I INT DEFAULT 1;\n"
        "  LBL_I_1: WHILE I <= 5 DO\n"
        "    IF I = 3 THEN\n"
        "      LEAVE LBL_I_1;\n"
        "    END IF;\n"
        "    SET I = I + 1;\n"
        "  END WHILE LBL_I_1;\n"
        "  LBL_LOOP_2: LOOP\n"
        "    LEAVE LBL_LOOP_2;\n"
        "  END LOOP LBL_LOOP_2;\n"
        "END;"
    )
    stmts = split_sql_statements(text)
    assert len(stmts) == 1
    assert "END WHILE LBL_I_1;" in stmts[0]
    assert "END LOOP LBL_LOOP_2;" in stmts[0]
