PROGRAM PLC_PRG
VAR_INPUT
    IN : BOOL;
    MAN : BOOL;
    M_I : BOOL;
    SET0 : BOOL;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : MANUAL_1;
    semantist_out_Q : BOOL;
    semantist_out_STATUS : BYTE;
END_VAR
semantist_fb(IN := IN, MAN := MAN, M_I := M_I, SET0 := SET0, RST := RST);
semantist_out_Q := semantist_fb.Q;
semantist_out_STATUS := semantist_fb.STATUS;
END_PROGRAM
