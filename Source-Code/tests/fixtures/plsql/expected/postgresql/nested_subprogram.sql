-- status: Requires manual conversion
-- issues: 2
--   [error] Nested PROCEDURE 'INNER_HELPER' is declared inside this routine's own DECLARE section; PL/pgSQL has no nested named-subprogram declaration -- extract it as a standalone function/procedure, or inline its logic by hand.
--   [info] Added CALL to the bare procedure-call statement 'INNER_HELPER(...)' -- Oracle allows an unqualified procedure invocation as a standalone statement, but PL/pgSQL requires the CALL keyword; left as-is this fails with a syntax error at the procedure's own name.

CREATE OR REPLACE PROCEDURE "outer_proc"(P_ID NUMERIC)
AS $$
DECLARE
  -- MANUAL CONVERSION REQUIRED: nested PROCEDURE INNER_HELPER, declared inside this routine's own DECLARE section -- PL/pgSQL has no nested named-subprogram declaration.
  -- Extract it as a standalone function/procedure, or inline its logic by hand. Original source:
  /*
PROCEDURE INNER_HELPER (P_X IN NUMBER) IS
  BEGIN
    V_COUNT := V_COUNT + P_X;
  END INNER_HELPER;
  */
  V_COUNT NUMERIC := 0;
  V_TOTAL NUMERIC;
BEGIN
  CALL INNER_HELPER(P_ID);
  V_TOTAL := V_COUNT;
END;
$$ LANGUAGE plpgsql;
