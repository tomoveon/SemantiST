(* CODESYS only; needs the OSCAT Basic library and CALIB_OSCAT_STR.
   Same scheme as CALIB_PRG: one step per cycle, RETAIN progress, an
   exception is recorded as "id=EXCEPTION" after reset warm and start.
   960 runs FILTER_I with T#1ms, 961 FILTER_I with T#0ms and 962 RMP_B with
   PT T#0ms, each for several cycles. *)

PROGRAM CALIB_OSCAT_PRG
VAR RETAIN
    next_step : INT := 1;
    running : INT;
    n : INT;
    out : ARRAY[1..20] OF STRING[80];
END_VAR
VAR
    restart : BOOL;
    done : BOOL;
    ids : ARRAY[1..6] OF INT := [951, 952, 953, 954, 955, 956];
    id : INT;
    cycles : INT;
    fi : FILTER_I;
    fi0 : FILTER_I;
    rb0 : RMP_B;
END_VAR

IF restart THEN
    restart := FALSE;
    next_step := 1;
    running := 0;
    n := 0;
    cycles := 0;
    done := FALSE;
END_IF;

IF running <> 0 THEN
    n := n + 1;
    out[n] := CONCAT(INT_TO_STRING(running), '=EXCEPTION');
    running := 0;
    next_step := next_step + 1;
    cycles := 0;
END_IF;

IF next_step <= 6 THEN
    id := ids[next_step];
    running := id;
    n := n + 1;
    out[n] := CONCAT(CONCAT(INT_TO_STRING(id), '=$''), CONCAT(CALIB_OSCAT_STR(id), '$''));
    running := 0;
    next_step := next_step + 1;
ELSIF next_step = 7 THEN
    running := 960;
    fi(X := 100, T := T#1ms);
    running := 0;
    cycles := cycles + 1;
    IF cycles = 50 THEN
        n := n + 1; out[n] := CONCAT('960 Y=', INT_TO_STRING(fi.Y));
        cycles := 0;
        next_step := 8;
    END_IF;
ELSIF next_step = 8 THEN
    running := 961;
    fi0(X := 100, T := T#0ms);
    running := 0;
    cycles := cycles + 1;
    IF cycles = 100 THEN
        n := n + 1; out[n] := CONCAT('961 Y=', INT_TO_STRING(fi0.Y));
        cycles := 0;
        next_step := 9;
    END_IF;
ELSIF next_step = 9 THEN
    running := 962;
    rb0(SET0 := FALSE, PT := T#0ms, E := TRUE, UP := TRUE, RST := FALSE);
    running := 0;
    cycles := cycles + 1;
    IF cycles = 100 THEN
        n := n + 1; out[n] := CONCAT('962 OUT=', BYTE_TO_STRING(rb0.OUT));
        cycles := 0;
        next_step := 10;
    END_IF;
ELSE
    done := TRUE;
END_IF;
END_PROGRAM
