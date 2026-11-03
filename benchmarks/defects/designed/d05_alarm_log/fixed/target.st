FUNCTION_BLOCK D05_ALARM_LOG
VAR_INPUT
    Alarm : BOOL;
    Code : INT;
    Clear : BOOL;
END_VAR
VAR_OUTPUT
    LastCode : INT;
    Logged : INT;
END_VAR
VAR
    log : ARRAY[0..9] OF INT;
    idx : INT;
    lastAlarm : BOOL;
END_VAR

IF Clear THEN
    idx := 0;
    Logged := 0;
ELSIF Alarm AND NOT lastAlarm AND Code > 0 THEN
    log[idx] := Code;
    LastCode := Code;
    Logged := Logged + 1;
    idx := idx + 1;
    IF idx > 9 THEN
        idx := 0;
    END_IF;
END_IF;
lastAlarm := Alarm;
END_FUNCTION_BLOCK
