-- status: Requires manual conversion
-- issues: 4
--   [error] Nested PROCEDURE 'INNER_HELPER' is declared inside this routine's own DECLARE section; Db2 SQL PL has no nested named-subprogram declaration -- extract it as a standalone procedure/function, or inline its logic by hand.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.

CREATE OR REPLACE PROCEDURE "OUTER_PROC"(
  IN P_ID DECIMAL(31,9)
)
LANGUAGE SQL
BEGIN
  -- MANUAL CONVERSION REQUIRED: nested PROCEDURE INNER_HELPER, declared inside this routine's own DECLARE section -- Db2 SQL PL has no nested named-subprogram declaration.
  -- Extract it as a standalone procedure/function, or inline its logic by hand. Original source:
  /*
  PROCEDURE INNER_HELPER (P_X IN NUMBER) IS
    BEGIN
      V_COUNT := V_COUNT + P_X;
    END INNER_HELPER;
  */
  DECLARE V_COUNT DECIMAL(31,9) DEFAULT 0;
  DECLARE V_TOTAL DECIMAL(31,9);

    INNER_HELPER(P_ID);
    SET V_TOTAL = V_COUNT;
END;
