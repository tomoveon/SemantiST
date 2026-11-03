FUNCTION DAYS_IN : INT
VAR_INPUT
    mth : INT;
    leap : BOOL;
END_VAR
(* OSCAT SET_DATE style: an IF inside a CASE branch, then ELSE: *)
CASE mth OF
    2:
        IF leap THEN
            DAYS_IN := 29;
        ELSE
            DAYS_IN := 28;
        END_IF;
    4, 6, 9, 11:
        DAYS_IN := 30;
ELSE(* : *)
    DAYS_IN := 31;
END_CASE;
END_FUNCTION

FUNCTION main : DINT
printf('%d %d %d %d$N', DAYS_IN(mth := 2, leap := TRUE), DAYS_IN(mth := 2, leap := FALSE),
       DAYS_IN(mth := 6, leap := FALSE), DAYS_IN(mth := 12, leap := FALSE));
main := 0;
END_FUNCTION
