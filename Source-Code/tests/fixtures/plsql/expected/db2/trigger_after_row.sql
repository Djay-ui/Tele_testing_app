-- status: Requires manual conversion
-- issues: 1
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).

CREATE OR REPLACE TRIGGER "TRG_BALANCE_CHECK"
AFTER UPDATE
ON "ACCOUNT"
REFERENCING NEW AS NEW_ROW OLD AS OLD_ROW
FOR EACH ROW
BEGIN ATOMIC

    IF NEW_ROW.BALANCE < 0 THEN

        DBMS_OUTPUT.PUT_LINE('Account overdrawn: ' || NEW_ROW.ACCOUNT_ID);
  
  END IF;
END;
