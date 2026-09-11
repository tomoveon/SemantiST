PROGRAM PLC_PRG
VAR_INPUT
    SET0 : BOOL;
    IN : DWORD;
    UP : BOOL;
    DN : BOOL;
    STEP : DWORD;
    MX : DWORD;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : COUNT_DR;
    semantist_out_CNT : DWORD;
END_VAR
semantist_fb(SET0 := SET0, IN := IN, UP := UP, DN := DN, STEP := STEP, MX := MX, RST := RST);
semantist_out_CNT := semantist_fb.CNT;
END_PROGRAM
