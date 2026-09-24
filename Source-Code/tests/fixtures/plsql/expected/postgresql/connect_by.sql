-- status: Converted automatically
-- issues: 3
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE.
--   [info] Explicitly declared REC as RECORD for use as an implicit-cursor FOR-loop target; Oracle never requires this declaration but it is emitted here for reliable PostgreSQL compilation.
--   [info] Hierarchical query (CONNECT BY) on EMPLOYEES was automatically rewritten as a recursive CTE (cb_cte_1) -- review the generated join/anchor conditions.

CREATE OR REPLACE PROCEDURE "print_org_chart"()
AS $$
DECLARE
  C1 CURSOR FOR WITH RECURSIVE cb_cte_1 (EMPLOYEE_ID, MANAGER_ID, LVL) AS (
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
  REC RECORD;
BEGIN
  FOR REC IN C1 LOOP
    RAISE NOTICE '%', RPAD(' ', REC.LVL * 2) || REC.EMPLOYEE_ID;
  END LOOP;
END;
$$ LANGUAGE plpgsql;
