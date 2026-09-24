-- status: Converted automatically
-- issues: 1
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE.

CREATE OR REPLACE PROCEDURE "greet"(P_NAME VARCHAR, OUT P_GREETING VARCHAR)
AS $$
DECLARE
  V_PREFIX VARCHAR(20) := 'Hello, ';
BEGIN
  P_GREETING := V_PREFIX || P_NAME || '!';
  RAISE NOTICE '%', P_GREETING;
END;
$$ LANGUAGE plpgsql;
