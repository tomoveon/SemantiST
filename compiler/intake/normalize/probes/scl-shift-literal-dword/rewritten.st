FUNCTION AIN_MASK : DWORD
VAR_INPUT
    bits : INT;
END_VAR
VAR
    temp1 : DWORD;
END_VAR
(* OSCAT AIN: a mask of the lowest bits *)
temp1 := SHR(IN:=(DWORD#16#FFFFFFFF),N:=32-bits);
AIN_MASK := temp1 OR SHL(IN := 16#1_0000, N := 1);
END_FUNCTION

FUNCTION main : DINT
printf('%u %u$N', AIN_MASK(bits := 8), AIN_MASK(bits := 20));
main := 0;
END_FUNCTION
