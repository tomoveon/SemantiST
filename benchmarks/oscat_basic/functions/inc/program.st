PROGRAM PLC_PRG
VAR_INPUT
    X : INT;
    D : INT;
    M : INT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := INC(X := X, D := D, M := M);
END_PROGRAM
