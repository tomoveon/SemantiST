PROGRAM PLC_PRG
VAR_INPUT
    SET0 : BOOL;
    PT : TIME;
    E : BOOL;
    UP : BOOL;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : RMP_B;
    semantist_out_OUT : BYTE;
    semantist_out_BUSY : BOOL;
    semantist_out_HIGH : BOOL;
    semantist_out_LOW : BOOL;
END_VAR
semantist_fb(SET0 := SET0, PT := PT, E := E, UP := UP, RST := RST);
semantist_out_OUT := semantist_fb.OUT;
semantist_out_BUSY := semantist_fb.BUSY;
semantist_out_HIGH := semantist_fb.HIGH;
semantist_out_LOW := semantist_fb.LOW;
END_PROGRAM
