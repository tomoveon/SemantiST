PROGRAM PLC_PRG
VAR_INPUT
    SEP : BYTE;
    RST : BOOL;
    LIST : STRING[LIST_LENGTH];
END_VAR
VAR
    semantist_fb : LIST_NEXT;
    semantist_out_LEL : STRING[LIST_LENGTH];
    semantist_out_NUL : BOOL;
END_VAR
semantist_fb(SEP := SEP, RST := RST, LIST := LIST);
semantist_out_LEL := semantist_fb.LEL;
semantist_out_NUL := semantist_fb.NUL;
END_PROGRAM
