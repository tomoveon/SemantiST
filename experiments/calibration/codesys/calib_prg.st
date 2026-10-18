(* CODESYS only. Runs one calibration case per cycle and collects the
   results in out[1..n] as "id=value".

   Progress is RETAIN: when a case raises an exception, reset warm and start
   again; the case is recorded as "id=EXCEPTION" and the run continues with
   the next one. 970 stands for the pointer and array steps H1-G2. Write
   restart := TRUE online to run everything again. *)

PROGRAM CALIB_PRG
VAR RETAIN
    next_step : INT := 1;
    running : INT;
    n : INT;
    out : ARRAY[1..100] OF STRING[80];
END_VAR
VAR
    restart : BOOL;
    done : BOOL;
    ids : ARRAY[1..86] OF INT := [
        101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118,
        201, 202, 203, 204, 205, 206, 207, 208,
        301, 302, 303, 304, 305, 306, 307, 308, 309,
        401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414, 415, 416,
        501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511, 512, 513, 514,
        601, 602,
        801, 802, 803, 804, 805, 806, 851, 852, 853,
        901, 902, 903, 904, 905, 906, 907, 908, 909, 910];
    id : INT;
    value : STRING[80];
    ab : ARRAY[0..3] OF BYTE := [1, 2, 3, 4];
    aw : ARRAY[0..3] OF WORD := [10, 20, 30, 40];
    pb : POINTER TO BYTE;
    pw : POINTER TO WORD;
    oob_arr : ARRAY[0..3] OF INT;
    oob_flag : INT;
    oob_k : INT := 4;
    t0 : TIME;
    cycles : INT;
END_VAR

IF restart THEN
    restart := FALSE;
    next_step := 1;
    running := 0;
    n := 0;
    done := FALSE;
END_IF;

(* The previous cycle stopped with an exception while running a case. *)
IF running <> 0 THEN
    n := n + 1;
    out[n] := CONCAT(INT_TO_STRING(running), '=EXCEPTION');
    running := 0;
    next_step := next_step + 1;
END_IF;

IF next_step <= 86 THEN
    id := ids[next_step];
    running := id;
    IF id >= 901 THEN
        value := CONCAT(CONCAT('$'', CALIB_STR(id)), '$'');
    ELSIF id >= 801 THEN
        value := LREAL_TO_STRING(CALIB_REAL(id));
    ELSE
        value := LINT_TO_STRING(CALIB_INT(id));
    END_IF;
    running := 0;
    n := n + 1;
    out[n] := CONCAT(CONCAT(INT_TO_STRING(id), '='), value);
    next_step := next_step + 1;
ELSIF next_step = 87 THEN
    running := 970;
    (* H: ADR arithmetic is in bytes? *)
    pb := ADR(ab) + 2;
    n := n + 1; out[n] := CONCAT('H1=', BYTE_TO_STRING(pb^));
    pw := ADR(aw) + 2;
    n := n + 1; out[n] := CONCAT('H2=', WORD_TO_STRING(pw^));
    pw := ADR(aw);
    pw := pw + 2;
    n := n + 1; out[n] := CONCAT('H3=', WORD_TO_STRING(pw^));
    (* G: out-of-bounds index inside one instance, no CheckBounds *)
    oob_flag := 0;
    oob_arr[oob_k] := 7;
    n := n + 1; out[n] := CONCAT('G1=', INT_TO_STRING(oob_flag));
    oob_flag := 9;
    n := n + 1; out[n] := CONCAT('G2=', INT_TO_STRING(oob_arr[oob_k]));
    running := 0;
    t0 := TIME();
    cycles := 0;
    n := n + 1; out[n] := CONCAT('T0=', DWORD_TO_STRING(TIME_TO_DWORD(t0)));
    next_step := 88;
ELSIF next_step = 88 THEN
    cycles := cycles + 1;
    IF cycles = 100 THEN
        n := n + 1; out[n] := CONCAT('T100=', DWORD_TO_STRING(TIME_TO_DWORD(TIME() - t0)));
        next_step := 89;
    END_IF;
ELSE
    done := TRUE;
END_IF;
END_PROGRAM
