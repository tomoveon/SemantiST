PROGRAM PLC_PRG
VAR_INPUT
    X : INT;
    D : INT;
    L : INT;
    U : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := INC2(X := X, D := D, L := L, U := U);
END_PROGRAM
