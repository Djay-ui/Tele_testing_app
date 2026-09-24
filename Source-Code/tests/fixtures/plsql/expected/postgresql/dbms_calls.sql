-- status: Converted automatically
-- issues: 2
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE.
--   [info] Converted 1 DBMS_RANDOM.VALUE reference(s) to random().

CREATE OR REPLACE PROCEDURE "util_calls"(P_CLOB VARCHAR)
AS $$
DECLARE
  V_LEN NUMERIC;
  V_PART VARCHAR(100);
  V_RAND NUMERIC;
BEGIN
  V_LEN := LENGTH(P_CLOB);
  V_PART := SUBSTR(P_CLOB, 5, 10);
  V_RAND := random();
  RAISE NOTICE '%', 'Length: ' || V_LEN;
END;
$$ LANGUAGE plpgsql;
