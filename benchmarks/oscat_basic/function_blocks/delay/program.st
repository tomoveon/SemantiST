PROGRAM PLC_PRG
VAR_INPUT
    IN : REAL;
    N : INT;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : DELAY;
    semantist_out_OUT : REAL;
END_VAR
semantist_fb(IN := IN, N := N, RST := RST);
semantist_out_OUT := semantist_fb.OUT;
END_PROGRAM
