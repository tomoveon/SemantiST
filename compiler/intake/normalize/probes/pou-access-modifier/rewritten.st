TYPE (* PUBLIC *)
    STEP : INT (
        ONE := 1,
        TWO := 2
    );
END_TYPE

FUNCTION_BLOCK (* PUBLIC *) COUNTER
VAR_OUTPUT
    N : INT;
END_VAR
N := N + STEP#ONE;
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    c : COUNTER;
END_VAR
c(); c();
printf('%d$N', c.N);
main := 0;
END_FUNCTION
