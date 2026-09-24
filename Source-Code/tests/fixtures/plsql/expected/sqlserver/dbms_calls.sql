-- status: Converted with warnings
-- issues: 5
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to PRINT.
--   [info] Converted 1 DBMS_RANDOM.VALUE reference(s) to RAND().
--   [info] Converted DBMS_LOB.GETLENGTH(...) to LEN(...); note T-SQL's LEN() trims trailing spaces, unlike Oracle's DBMS_LOB.GETLENGTH -- use DATALENGTH(...) instead if trailing whitespace must be counted.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.

CREATE OR ALTER PROCEDURE [UTIL_CALLS]
  @P_CLOB VARCHAR(MAX)
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @V_LEN DECIMAL(38,10);
  DECLARE @V_PART VARCHAR(100);
  DECLARE @V_RAND DECIMAL(38,10);

    SET @V_LEN = LEN(@P_CLOB);
    SET @V_PART = SUBSTRING(@P_CLOB, 5, 10);
    SET @V_RAND = RAND();
    PRINT ('Length: ' || @V_LEN);
END;
