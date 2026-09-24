-- status: Converted with warnings
-- issues: 2
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to PRINT.
--   [warning] Trigger 'TRG_BALANCE_CHECK' is FOR EACH ROW in Oracle; SQL Server triggers are always statement-level and operate on the `inserted`/`deleted` pseudo-tables, which may contain zero, one, or many rows per statement. :NEW/:OLD references below were rewritten as single-row lookups against inserted/deleted, which is only correct for single-row DML -- rewrite this trigger to be fully set-based if multi-row DML against this table is expected.

CREATE OR ALTER TRIGGER [TRG_BALANCE_CHECK]
ON [ACCOUNT]
AFTER UPDATE
AS
BEGIN
  SET NOCOUNT ON;

    IF (SELECT BALANCE FROM inserted) < 0
  BEGIN

        PRINT ('Account overdrawn: ' || (SELECT ACCOUNT_ID FROM inserted));
  
  END
END;
