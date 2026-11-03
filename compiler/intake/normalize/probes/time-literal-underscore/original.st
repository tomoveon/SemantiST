FUNCTION main : DINT
VAR
    a : TIME := T#2s_750ms;
    b : TIME := T#24d_20h_31m_23s_647ms;
END_VAR
printf('%lld %lld$N', TIME_TO_LINT(a) / 1000000, TIME_TO_LINT(b) / 1000000);
main := 0;
END_FUNCTION
