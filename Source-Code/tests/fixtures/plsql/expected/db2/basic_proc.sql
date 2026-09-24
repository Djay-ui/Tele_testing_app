-- status: Requires manual conversion
-- issues: 1
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).

CREATE OR REPLACE PROCEDURE "GREET"(
  IN P_NAME VARCHAR(4000),
  OUT P_GREETING VARCHAR(4000)
)
LANGUAGE SQL
BEGIN
  DECLARE V_PREFIX VARCHAR(20) DEFAULT 'Hello, ';

    SET P_GREETING = V_PREFIX || P_NAME || '!';
    DBMS_OUTPUT.PUT_LINE(P_GREETING);
END;
