-- status: Converted automatically
-- issues: 3
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to PRINT.
--   [info] Converted implicit-cursor FOR loop 'REC' into an explicit CURSOR/FETCH/WHILE loop; fetched column(s) EMPLOYEE_ID, MANAGER_ID, LVL were declared as SQL_VARIANT -- tighten these to the actual column types for best performance and type safety.
--   [info] Hierarchical query (CONNECT BY) on EMPLOYEES was automatically rewritten as a recursive CTE (cb_cte_1) -- review the generated join/anchor conditions.

CREATE OR ALTER PROCEDURE [PRINT_ORG_CHART]
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE C1 CURSOR LOCAL FAST_FORWARD FOR
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

  
  DECLARE @EMPLOYEE_ID SQL_VARIANT, @MANAGER_ID SQL_VARIANT, @LVL SQL_VARIANT;
  OPEN C1;
  FETCH NEXT FROM C1 INTO @EMPLOYEE_ID, @MANAGER_ID, @LVL;
  WHILE @@FETCH_STATUS = 0
  BEGIN


          PRINT (RPAD(' ', @LVL * 2) || @EMPLOYEE_ID);
  
      FETCH NEXT FROM C1 INTO @EMPLOYEE_ID, @MANAGER_ID, @LVL;
  END
  CLOSE C1;
  DEALLOCATE C1;

END;
