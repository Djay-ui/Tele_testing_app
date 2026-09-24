-- status: Requires manual conversion
-- issues: 6
--   [error] Exception 'DIVIDE_BY_ZERO_LOCAL' is declared but Db2 has no equivalent to an Oracle user-defined EXCEPTION; RAISE statements referencing it are converted to SIGNAL SQLSTATE '70000' with the exception's name as the message -- review and assign a real SQLSTATE/message.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(31,9) (Db2's maximum precision is 31); verify range requirements on the target.
--   [warning] RAISE_APPLICATION_ERROR error code -20001 was dropped; Db2 SIGNAL uses a 5-character SQLSTATE instead -- a generic user-defined SQLSTATE ('70000') was used; assign a specific one (in the '70000'-'99999' or 'U0000'-'U9999' user-defined ranges) if the caller depends on it.

CREATE OR REPLACE FUNCTION "SAFE_DIV"(
  IN P_NUMERATOR DECIMAL(31,9),
  IN P_DENOMINATOR DECIMAL(31,9)
)
RETURNS DECIMAL(31,9)
LANGUAGE SQL
BEGIN
  DECLARE V_RESULT DECIMAL(31,9);
  -- (kept for reference only) DIVIDE_BY_ZERO_LOCAL EXCEPTION;  -- Db2 has no user-defined EXCEPTION type
  DECLARE EXIT HANDLER FOR SQLSTATE '22012'
  BEGIN

        RETURN NULL;
  
  END;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN

        SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'Unexpected error in SAFE_DIV';
  END;

    IF P_DENOMINATOR = 0 THEN

        SIGNAL SQLSTATE '70000' SET MESSAGE_TEXT = 'DIVIDE_BY_ZERO_LOCAL';
  
  END IF;
    SET V_RESULT = P_NUMERATOR / P_DENOMINATOR;
    RETURN V_RESULT;
END;
