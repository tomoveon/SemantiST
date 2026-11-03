FUNCTION SCALE : INT
VAR_INPUT
    X : INT;
END_VAR
VAR
    t : INT;
END_VAR
    (* REGION Türverriegelung prüfen *)
        t := X * 2;
        (* REGION Offset *)
            t := t + 3;
        (* END_REGION *)
    (* END_REGION *)
    SCALE := t;
END_FUNCTION

FUNCTION main : DINT
printf('%d$N', SCALE(10));
main := 0;
END_FUNCTION
