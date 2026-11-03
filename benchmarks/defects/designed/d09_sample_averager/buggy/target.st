FUNCTION_BLOCK D09_SAMPLE_AVERAGER
VAR_INPUT
    Sample : BOOL;
    Value : INT;
    Reset : BOOL;
END_VAR
VAR_OUTPUT
    Average : INT;
    Ready : BOOL;
END_VAR
VAR
    buf : ARRAY[0..7] OF INT;
    wr : INT;
    count : INT;
    sum : DINT;
    i : INT;
    lastSample : BOOL;
END_VAR

Ready := FALSE;
IF Reset THEN
    (* defect: wr is not reset together with count *)
    count := 0;
ELSIF Sample AND NOT lastSample AND count < 8 THEN
    buf[wr] := Value;
    wr := wr + 1;
    count := count + 1;
END_IF;
IF count = 8 THEN
    sum := 0;
    FOR i := 0 TO 7 DO
        sum := sum + buf[i];
    END_FOR;
    Average := DINT_TO_INT(sum / 8);
    Ready := TRUE;
    count := 0;
    wr := 0;
END_IF;
lastSample := Sample;
END_FUNCTION_BLOCK
