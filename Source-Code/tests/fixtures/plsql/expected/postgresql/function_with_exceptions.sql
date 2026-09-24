-- status: Converted with warnings
-- issues: 2
--   [warning] Exception 'DIVIDE_BY_ZERO_LOCAL' is declared but Postgres has no user-defined EXCEPTION type; every RAISE and WHEN referencing it in this routine's body was rewritten to use a synthetic SQLSTATE code instead (see convert_body) -- review only if code outside this routine needs to catch it by a *specific*, stable SQLSTATE of its own choosing.
--   [warning] RAISE_APPLICATION_ERROR error code -20001 was dropped; Postgres uses SQLSTATE codes instead — add `USING ERRCODE = '...'` if the caller depends on the specific code.

CREATE OR REPLACE FUNCTION "safe_div"(P_NUMERATOR NUMERIC, P_DENOMINATOR NUMERIC)
RETURNS NUMERIC AS $$
DECLARE
  V_RESULT NUMERIC;
  -- (kept for reference only) DIVIDE_BY_ZERO_LOCAL EXCEPTION;  -- Postgres has no user-defined EXCEPTION type
BEGIN
  IF P_DENOMINATOR = 0 THEN
    RAISE EXCEPTION 'DIVIDE_BY_ZERO_LOCAL' USING ERRCODE = 'U0001';
  END IF;
  V_RESULT := P_NUMERATOR / P_DENOMINATOR;
  RETURN V_RESULT;
EXCEPTION
  WHEN division_by_zero THEN
    RETURN NULL;
  WHEN OTHERS THEN
    RAISE EXCEPTION USING MESSAGE = 'Unexpected error in SAFE_DIV';
END;
$$ LANGUAGE plpgsql;
