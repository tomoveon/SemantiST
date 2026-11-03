(* OSCAT's ROUND(in, N) rounds to N decimal places; RuSTy's ROUND takes one argument. *)
FUNCTION ROUND__ : REAL
VAR_INPUT
    in : REAL;
    N : INT;
END_VAR
ROUND__ := DINT_TO_REAL(REAL_TO_DINT(in * EXPT(10.0, N))) / EXPT(10.0, N);
END_FUNCTION

FUNCTION main : DINT
printf('%d$N', REAL_TO_DINT(ROUND__(3.14159, 2) * 1000.0));
main := 0;
END_FUNCTION
