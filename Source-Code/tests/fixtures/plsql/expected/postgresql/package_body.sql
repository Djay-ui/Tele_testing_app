-- status: Converted with warnings
-- issues: 3
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to RAISE NOTICE.
--   [info] Explicitly declared REC as RECORD for use as an implicit-cursor FOR-loop target; Oracle never requires this declaration but it is emitted here for reliable PostgreSQL compilation.
--   [warning] Package-level variables/constants (if the package declared any) have no direct Postgres equivalent; consider a settings table or session variables (SET/current_setting) if state needs to be shared across the flattened functions.

-- Flattened from PACKAGE BODY PKG_BANK (2 member(s)). Package-level state (if any) is not carried over — see notes below.

CREATE OR REPLACE PROCEDURE "pkg_bank_get_customer"(P_CUSTOMER_ID NUMERIC)
AS $$
DECLARE
  REC RECORD;
BEGIN
    FOR REC IN (SELECT * FROM CUSTOMER WHERE CUSTOMER_ID = P_CUSTOMER_ID) LOOP
      RAISE NOTICE '%', 'Customer ID: ' || REC.CUSTOMER_ID;
    END LOOP;
  END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "pkg_bank_get_total_customers"()
RETURNS NUMERIC AS $$
DECLARE
  V_TOTAL NUMERIC;
BEGIN
    SELECT COUNT(*) INTO V_TOTAL FROM CUSTOMER;
    RETURN V_TOTAL;
  END;
$$ LANGUAGE plpgsql;
