-- status: Requires manual conversion
-- issues: 4
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).
--   [info] Converted 1 DBMS_RANDOM.VALUE reference(s) to RAND().
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.

CREATE OR REPLACE PROCEDURE "UTIL_CALLS"(
  IN P_CLOB VARCHAR(4000)
)
LANGUAGE SQL
BEGIN
  DECLARE V_LEN DECIMAL(31,9);
  DECLARE V_PART VARCHAR(100);
  DECLARE V_RAND DECIMAL(31,9);

    SET V_LEN = LENGTH(P_CLOB);
    SET V_PART = SUBSTR(P_CLOB, 5, 10);
    SET V_RAND = RAND();
    DBMS_OUTPUT.PUT_LINE('Length: ' || V_LEN);
END;
