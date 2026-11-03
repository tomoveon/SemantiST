TYPE SLOT : STRUCT
    NAME : STRING(10);
    DAY : SINT;
    USE : SINT := 7;
END_STRUCT
END_TYPE

VAR_GLOBAL
    TABLE : ARRAY[0..3] OF SLOT := [(NAME := 'a', DAY := 1, USE := 1), (USE := 7), (NAME := 'b', DAY := 2, USE := 0), (USE := 7)];
END_VAR

FUNCTION main : DINT
VAR
    local : ARRAY[0..2] OF SLOT := [(USE := 7), (DAY := 5), (USE := 7)];
END_VAR
printf('%d %d %d %d %d$N', TABLE[1].DAY, TABLE[1].USE, TABLE[2].DAY, TABLE[3].USE, LEN(TABLE[3].NAME));
printf('%d %d %d$N', local[0].USE, local[1].DAY, local[2].USE);
main := 0;
END_FUNCTION
