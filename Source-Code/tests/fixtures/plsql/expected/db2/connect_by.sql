-- status: Requires manual conversion
-- issues: 3
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).
--   [info] Hierarchical query (CONNECT BY) on EMPLOYEES was automatically rewritten as a recursive CTE (cb_cte_1) -- review the generated join/anchor conditions.

CREATE OR REPLACE PROCEDURE "PRINT_ORG_CHART"()
LANGUAGE SQL
BEGIN
  DECLARE C1 CURSOR FOR
    WITH cb_cte_1 (EMPLOYEE_ID, MANAGER_ID, LVL) AS (
    SELECT EMPLOYEE_ID, MANAGER_ID, 1
    FROM EMPLOYEES
    WHERE MANAGER_ID IS NULL
    UNION ALL
    SELECT _cb_t.EMPLOYEE_ID, _cb_t.MANAGER_ID, cb_cte_1.LVL + 1
    FROM EMPLOYEES _cb_t
    JOIN cb_cte_1 ON _cb_t.MANAGER_ID = cb_cte_1.EMPLOYEE_ID
  )
  SELECT EMPLOYEE_ID, MANAGER_ID, LVL
  FROM cb_cte_1;

  
  LBL_REC_1: FOR REC AS C1 CURSOR FOR
    WITH cb_cte_1 (EMPLOYEE_ID, MANAGER_ID, LVL) AS (
    SELECT EMPLOYEE_ID, MANAGER_ID, 1
    FROM EMPLOYEES
    WHERE MANAGER_ID IS NULL
    UNION ALL
    SELECT _cb_t.EMPLOYEE_ID, _cb_t.MANAGER_ID, cb_cte_1.LVL + 1
    FROM EMPLOYEES _cb_t
    JOIN cb_cte_1 ON _cb_t.MANAGER_ID = cb_cte_1.EMPLOYEE_ID
  )
  SELECT EMPLOYEE_ID, MANAGER_ID, LVL
  FROM cb_cte_1
  DO

        DBMS_OUTPUT.PUT_LINE(RPAD(' ', REC.LVL * 2) || REC.EMPLOYEE_ID);
  
  END FOR LBL_REC_1;

END;
