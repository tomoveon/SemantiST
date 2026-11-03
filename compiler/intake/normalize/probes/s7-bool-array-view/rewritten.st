FUNCTION_BLOCK STIME
VAR_INPUT
    raw : DWORD;
    flag : BOOL;
END_VAR
VAR_OUTPUT
    tx : DWORD;
    (* at_tx AT tx : ARRAY[0..31] OF BOOL; *)
    status : BYTE;
    (* bits AT status : ARRAY[0..7] OF BOOL; *)
    low : BOOL;
    msb_of_byte0 : BOOL;
END_VAR
tx := raw;
(* stuff the highest bit into the timer value (OSCAT STIME) *)
tx.%X31 := flag;
low := tx.%X0;
msb_of_byte0 := tx.%X24;
status.%X0 := TRUE;
status.%X7 := tx.%X7;
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    s : STIME;
END_VAR
s(raw := DWORD#16#00000001, flag := TRUE);
printf('%u %d %d %u$N', s.tx, s.low, s.msb_of_byte0, s.status);
s(raw := DWORD#16#01000000, flag := FALSE);
printf('%u %d %d %u$N', s.tx, s.low, s.msb_of_byte0, s.status);
main := 0;
END_FUNCTION
