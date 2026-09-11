PROGRAM PLC_PRG
VAR_INPUT
    C : BYTE;
END_VAR
VAR
    semantist_result : STRING[10];
END_VAR
semantist_result := CHARNAME(C := C);
END_PROGRAM
