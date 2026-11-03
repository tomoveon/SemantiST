FUNCTION_BLOCK D06_SERVICE_COUNTER
VAR_INPUT
    Run : BOOL;
    Speed : INT;
    ResetService : BOOL;
END_VAR
VAR_OUTPUT
    Hours : INT;
    ServiceDue : BOOL;
    Interval : INT;
END_VAR
VAR
    plan : ARRAY[0..11] OF INT := [50, 50, 100, 100, 100, 200, 200, 200, 400, 400, 400, 800];
END_VAR

IF ResetService THEN
    Hours := 0;
ELSIF Run AND Speed >= 1450 AND Speed <= 1550 THEN
    Hours := Hours + 1;
END_IF;
IF Hours >= 10 THEN
    Interval := plan[MIN(Hours / 10, 11)];
    ServiceDue := Hours MOD 10 = 0;
END_IF;
END_FUNCTION_BLOCK
