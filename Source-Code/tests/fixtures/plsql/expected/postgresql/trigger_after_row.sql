-- status: Converted with warnings
-- issues: 2
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE.
--   [warning] No RETURN found in the trigger body; added `RETURN NULL;` before the end — verify this matches the intended trigger semantics.

CREATE OR REPLACE FUNCTION "trg_balance_check_fn"()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.BALANCE < 0 THEN
    RAISE NOTICE '%', 'Account overdrawn: ' || NEW.ACCOUNT_ID;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS "trg_balance_check" ON "account";
CREATE TRIGGER "trg_balance_check"
AFTER UPDATE ON "account"
FOR EACH ROW
EXECUTE FUNCTION "trg_balance_check_fn"();
