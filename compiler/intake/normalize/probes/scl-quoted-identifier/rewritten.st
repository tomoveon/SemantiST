FUNCTION Scale_Value__Q : INT
VAR_INPUT
    X : INT;
END_VAR
Scale_Value__Q := X * 5;
END_FUNCTION

FUNCTION main : DINT
printf('%d$N', Scale_Value__Q(3));
main := 0;
END_FUNCTION
