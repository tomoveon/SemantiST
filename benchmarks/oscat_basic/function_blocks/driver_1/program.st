PROGRAM PLC_PRG
VAR_INPUT
    Toggle_Mode : BOOL;
    Timeout : TIME;
    SET0 : BOOL;
    IN : BOOL;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : DRIVER_1;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(Toggle_Mode := Toggle_Mode, Timeout := Timeout, SET0 := SET0, IN := IN, RST := RST);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM
