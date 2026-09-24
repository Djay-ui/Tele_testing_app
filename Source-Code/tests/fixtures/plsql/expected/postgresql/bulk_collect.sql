-- status: Requires manual conversion
-- issues: 1
--   [error] PROCEDURE GET_EMPLOYEE_IDS: variable(s) V_IDS, V_NAMES use a locally-declared Oracle collection/record/REF CURSOR type with no direct Postgres equivalent; this routine's body almost certainly relies on Oracle-only element access (e.g. 'V_IDS(i) := ...') or collection methods (.COUNT/.FIRST/.LAST/.EXISTS/.DELETE) that cannot be mechanically rewritten -- redesign using a Postgres array, composite type, or refcursor variable and rewrite the whole routine by hand.

-- MANUAL CONVERSION REQUIRED for PROCEDURE GET_EMPLOYEE_IDS
-- Uses a locally-declared Oracle collection/record/REF CURSOR type (V_IDS, V_NAMES) with no direct Postgres equivalent -- see the assessment report for details.
/*
PROCEDURE GET_EMPLOYEE_IDS (P_DEPT_ID IN NUMBER) IS
  TYPE T_ID_LIST IS TABLE OF NUMBER;
  V_IDS T_ID_LIST;
  V_NAMES T_ID_LIST;
BEGIN
  SELECT EMP_ID, EMP_NAME BULK COLLECT INTO V_IDS, V_NAMES
    FROM EMPLOYEES WHERE DEPT_ID = P_DEPT_ID;
END GET_EMPLOYEE_IDS;

*/
