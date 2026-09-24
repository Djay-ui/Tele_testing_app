-- status: Requires manual conversion
-- issues: 7
--   [error] Exception 'DIVIDE_BY_ZERO_LOCAL' is declared but T-SQL has no equivalent to an Oracle user-defined EXCEPTION; RAISE statements referencing it are converted to THROW 50000 with the exception's name as the message -- review and assign a real error number/message.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] NUMBER with no precision/scale mapped to DECIMAL(38,10) (SQL Server's maximum precision is 38); verify range requirements on the target.
--   [warning] RAISE_APPLICATION_ERROR error code -20001 was dropped; T-SQL THROW requires a user error number >= 50000 -- add one (and register it with sys.sp_addmessage if a specific number must be preserved) if the caller depends on it.
--   [warning] T-SQL scalar functions cannot contain PRINT, THROW/RAISERROR, DML statements, or EXEC calls; review the generated body and relocate any such logic (e.g. into a calling procedure) -- SQL Server will reject the CREATE FUNCTION otherwise.

CREATE OR ALTER FUNCTION [SAFE_DIV](
  @P_NUMERATOR DECIMAL(38,10),
  @P_DENOMINATOR DECIMAL(38,10)
)
RETURNS DECIMAL(38,10)
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @V_RESULT DECIMAL(38,10);
  -- (kept for reference only) DIVIDE_BY_ZERO_LOCAL EXCEPTION;  -- T-SQL has no user-defined EXCEPTION type
  BEGIN TRY

      IF @P_DENOMINATOR = 0
    BEGIN

          THROW 50000, 'DIVIDE_BY_ZERO_LOCAL', 1;
  
    END
      SET @V_RESULT = @P_NUMERATOR / @P_DENOMINATOR;
      RETURN @V_RESULT;
  END TRY
  BEGIN CATCH
    IF ERROR_NUMBER() IN (8134)
    BEGIN

          RETURN NULL;
  
    END
    ELSE
    BEGIN

          THROW 50000, 'Unexpected error in SAFE_DIV', 1;
    END
  END CATCH
END;
