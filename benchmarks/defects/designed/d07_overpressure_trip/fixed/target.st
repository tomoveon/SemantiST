FUNCTION_BLOCK D07_OVERPRESSURE_TRIP
VAR_INPUT
    Pressure : INT;
    Ack : BOOL;
END_VAR
VAR_OUTPUT
    Trip : BOOL;
    Trips : INT;
END_VAR
VAR
    overTimer : TON;
    tripLog : ARRAY[1..3] OF INT;
    lastQ : BOOL;
END_VAR

overTimer(IN := Pressure > 800, PT := T#500ms);
IF overTimer.Q AND NOT lastQ THEN
    IF Trips < 3 THEN
        Trips := Trips + 1;
        tripLog[Trips] := Pressure;
    END_IF;
    Trip := TRUE;
END_IF;
lastQ := overTimer.Q;
IF Ack AND NOT overTimer.Q THEN
    Trip := FALSE;
END_IF;
END_FUNCTION_BLOCK
