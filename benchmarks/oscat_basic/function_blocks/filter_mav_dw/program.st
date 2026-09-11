PROGRAM PLC_PRG
VAR_INPUT
    X : DWORD;
    N : UINT;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : FILTER_MAV_DW;
    semantist_out_Y : DWORD;
END_VAR
semantist_fb(X := X, N := N, RST := RST);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM
