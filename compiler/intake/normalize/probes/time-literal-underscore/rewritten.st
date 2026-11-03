FUNCTION main : DINT
VAR
    a : TIME := T#2s750ms;
    b : TIME := T#24d20h31m23s647ms;
END_VAR
printf('%lld %lld$N', TIME_TO_LINT(a) / 1000000, TIME_TO_LINT(b) / 1000000);
main := 0;
END_FUNCTION
