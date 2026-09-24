-- status: Requires manual conversion
-- issues: 7
--   [error] Could not parse the routine body's BEGIN/END structure; manual conversion required.
--   [error] MANUAL CONVERSION REQUIRED: implicit-cursor FOR loop 'REC' uses SELECT * or an unaliased expression column, so its fetched columns can't be named automatically; give every selected column an explicit alias and re-run, or rewrite this loop by hand as an explicit CURSOR/FETCH/WHILE @@FETCH_STATUS loop.
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to PRINT.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] Package-level variables/constants (if the package declared any) have no direct T-SQL equivalent; consider a settings table or SESSION_CONTEXT if state needs to be shared across the flattened procedures/functions.

-- Flattened from PACKAGE BODY PKG_BANK (2 member(s)). Package-level state (if any) is not carried over -- see notes below.

CREATE OR ALTER PROCEDURE [PKG_BANK_GET_CUSTOMER]
  @P_CUSTOMER_ID DECIMAL(38,10)
AS
BEGIN
  SET NOCOUNT ON;
  -- MANUAL CONVERSION REQUIRED
  /*
  BEGIN
    
    /* MANUAL CONVERSION REQUIRED -- original Oracle loop follows:
  FOR REC IN (SELECT * FROM CUSTOMER WHERE CUSTOMER_ID = @P_CUSTOMER_ID) LOOP
        PRINT ('Customer ID: ' || REC.CUSTOMER_ID);
      END LOOP;
    */

    END;
  */
END;

CREATE OR ALTER FUNCTION [PKG_BANK_GET_TOTAL_CUSTOMERS]()
RETURNS DECIMAL(38,10)
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @V_TOTAL DECIMAL(38,10);

      SELECT @V_TOTAL = COUNT(*) FROM CUSTOMER;
      RETURN @V_TOTAL;
  
END;
