FUNCTION main : DINT
VAR
    w : WORD;
    d : DWORD;
END_VAR
w := W#16#00FF;
d := DW#16#0001_0000;
printf('%d %d$N', WORD_TO_DINT(w), DWORD_TO_DINT(d));
main := 0;
END_FUNCTION
