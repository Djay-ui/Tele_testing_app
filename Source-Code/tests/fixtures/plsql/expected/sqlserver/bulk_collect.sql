-- status: Requires manual conversion
-- issues: 5
--   [error] BULK COLLECT has no direct equivalent; rewrite using a table variable or a loop.
--   [error] Local type declaration 'TYPE T_ID_LIST IS TABLE OF NUMBER;' (TABLE OF / RECORD / REF CURSOR) has no direct T-SQL equivalent -- rewrite using a table variable, a user-defined table type, or a cursor variable as appropriate, and update every reference to it in this routine's body.
--   [error] No mapping rule for Oracle type 'T_ID_LIST'. Defaulting to NVARCHAR(MAX); manual review required.
--   [error] No mapping rule for Oracle type 'T_ID_LIST'. Defaulting to NVARCHAR(MAX); manual review required.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.

CREATE OR ALTER PROCEDURE [GET_EMPLOYEE_IDS]
  @P_DEPT_ID DECIMAL(38,10)
AS
BEGIN
  SET NOCOUNT ON;
  -- MANUAL CONVERSION REQUIRED: TYPE T_ID_LIST IS TABLE OF NUMBER;
  DECLARE @V_IDS NVARCHAR(MAX);
  DECLARE @V_NAMES NVARCHAR(MAX);

    SELECT @V_IDS = EMP_ID, @V_NAMES = EMP_NAME BULK COLLECT FROM EMPLOYEES WHERE DEPT_ID = @P_DEPT_ID;
END;
