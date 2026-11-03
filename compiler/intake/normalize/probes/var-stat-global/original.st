FUNCTION_BLOCK COUNTER
VAR_STAT
    calls : INT;
END_VAR
VAR_OUTPUT
    seen : INT;
END_VAR
calls := calls + 1;
seen := calls;
END_FUNCTION_BLOCK

FUNCTION CRC_OF : INT
VAR_INPUT
    i : INT;
END_VAR
VAR_STAT CONSTANT
    TABLE : ARRAY[0..2] OF INT := [7, 11, 13];
END_VAR
CRC_OF := TABLE[i];
END_FUNCTION

FUNCTION main : DINT
VAR
    a : COUNTER;
    b : COUNTER;
END_VAR
a();
b();
a();
printf('%d %d %d$N', a.seen, b.seen, CRC_OF(2));
main := 0;
END_FUNCTION
