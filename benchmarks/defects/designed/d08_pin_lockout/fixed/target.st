FUNCTION_BLOCK D08_PIN_LOCKOUT
VAR_INPUT
    Enter : BOOL;
    Code : INT;
END_VAR
VAR_OUTPUT
    Unlocked : BOOL;
    Locked : BOOL;
END_VAR
VAR
    lockTimer : TON;
    failures : INT;
    attempts : ARRAY[0..3] OF INT;
    lastEnter : BOOL;
END_VAR

lockTimer(IN := Locked, PT := T#500ms);
IF lockTimer.Q THEN
    Locked := FALSE;
    failures := 0;
END_IF;
IF Enter AND NOT lastEnter AND NOT Locked THEN
    IF Code = 4711 THEN
        Unlocked := TRUE;
        failures := 0;
    ELSE
        Unlocked := FALSE;
        attempts[failures] := Code;
        failures := failures + 1;
        IF failures >= 3 THEN
            Locked := TRUE;
        END_IF;
    END_IF;
END_IF;
lastEnter := Enter;
END_FUNCTION_BLOCK
