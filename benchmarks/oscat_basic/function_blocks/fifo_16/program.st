PROGRAM PLC_PRG
VAR_INPUT
    Din : DWORD;
    E : BOOL;
    RD : BOOL;
    WD : BOOL;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : FIFO_16;
    semantist_out_Dout : DWORD;
    semantist_out_EMPTY : BOOL;
    semantist_out_FULL : BOOL;
END_VAR
semantist_fb(Din := Din, E := E, RD := RD, WD := WD, RST := RST);
semantist_out_Dout := semantist_fb.Dout;
semantist_out_EMPTY := semantist_fb.EMPTY;
semantist_out_FULL := semantist_fb.FULL;
END_PROGRAM
