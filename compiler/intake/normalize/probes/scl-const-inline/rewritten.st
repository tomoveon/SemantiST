FUNCTION CIRCLE : REAL
VAR_INPUT
    R : REAL;
END_VAR
(* CONST
    PI1 := 3.14;
    TWICE := 2 * 25;
END_CONST *)
CIRCLE := (3.14) * R * R;
END_FUNCTION

FUNCTION main : DINT
printf('%d$N', REAL_TO_DINT(CIRCLE(10.0)));
printf('%d$N', 2 * 25);
main := 0;
END_FUNCTION
