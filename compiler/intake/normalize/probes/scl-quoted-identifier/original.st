FUNCTION "Scale Value" : INT
VAR_INPUT
    X : INT;
END_VAR
"Scale Value" := X * 5;
END_FUNCTION

FUNCTION main : DINT
printf('%d$N', "Scale Value"(3));
main := 0;
END_FUNCTION
