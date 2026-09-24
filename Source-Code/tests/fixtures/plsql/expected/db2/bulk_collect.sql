-- status: Requires manual conversion
-- issues: 5
--   [error] BULK COLLECT has no direct equivalent; rewrite using an array data type or a loop.
--   [error] Local type declaration 'TYPE T_ID_LIST IS TABLE OF NUMBER;' (TABLE OF / RECORD / REF CURSOR) has no direct Db2 SQL PL equivalent -- rewrite using an array type, a row type, or a cursor variable as appropriate, and update every reference to it in this routine's body.
--   [error] No mapping rule for Oracle type 'T_ID_LIST'. Defaulting to CLOB; manual review required.
--   [error] No mapping rule for Oracle type 'T_ID_LIST'. Defaulting to CLOB; manual review required.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.

CREATE OR REPLACE PROCEDURE "GET_EMPLOYEE_IDS"(
  IN P_DEPT_ID DECIMAL(31,9)
)
LANGUAGE SQL
BEGIN
  -- MANUAL CONVERSION REQUIRED: TYPE T_ID_LIST IS TABLE OF NUMBER;
  DECLARE V_IDS CLOB;
  DECLARE V_NAMES CLOB;

    SELECT EMP_ID, EMP_NAME BULK COLLECT INTO V_IDS, V_NAMES
      FROM EMPLOYEES WHERE DEPT_ID = P_DEPT_ID;
END;
