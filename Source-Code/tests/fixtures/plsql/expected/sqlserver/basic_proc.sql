-- status: Converted automatically
-- issues: 1
--   [info] Converted 1 DBMS_OUTPUT.PUT_LINE call(s) to PRINT.

CREATE OR ALTER PROCEDURE [GREET]
  @P_NAME VARCHAR(MAX),
  @P_GREETING VARCHAR(MAX) OUTPUT
AS
BEGIN
  SET NOCOUNT ON;
  DECLARE @V_PREFIX VARCHAR(20) = 'Hello, ';

    SET @P_GREETING = @V_PREFIX || @P_NAME || '!';
    PRINT (@P_GREETING);
END;
