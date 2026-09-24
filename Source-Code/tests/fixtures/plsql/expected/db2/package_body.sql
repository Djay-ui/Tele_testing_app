-- status: Requires manual conversion
-- issues: 6
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).
--   [error] Uses an Oracle DBMS_* package with no direct Db2 equivalent (Db2 SQL PL has no console-output statement to substitute for DBMS_OUTPUT.PUT_LINE either).
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] Package-level variables/constants (if the package declared any) have no direct Db2 equivalent; consider a global variable (CREATE VARIABLE) or a settings table if state needs to be shared across the flattened procedures/functions.

-- Flattened from PACKAGE BODY PKG_BANK (2 member(s)). Package-level state (if any) is not carried over -- see notes below.

CREATE OR REPLACE PROCEDURE "PKG_BANK_GET_CUSTOMER"(
  IN P_CUSTOMER_ID DECIMAL(31,9)
)
LANGUAGE SQL
BEGIN

    
  LBL_REC_1: FOR REC AS REC_cur CURSOR FOR
    SELECT * FROM CUSTOMER WHERE CUSTOMER_ID = P_CUSTOMER_ID
  DO

          DBMS_OUTPUT.PUT_LINE('Customer ID: ' || REC.CUSTOMER_ID);
    
  END FOR LBL_REC_1;

  
END;

CREATE OR REPLACE FUNCTION "PKG_BANK_GET_TOTAL_CUSTOMERS"()
RETURNS DECIMAL(31,9)
LANGUAGE SQL
BEGIN
  DECLARE V_TOTAL DECIMAL(31,9);

      SELECT COUNT(*) INTO V_TOTAL FROM CUSTOMER;
      RETURN V_TOTAL;
  
END;
