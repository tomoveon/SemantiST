PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    iter : INT;
    Input : POINTER TO LREAL;
    ByteNum : DWORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_SMART_OOBW_1;
    semantist_out_Out : ARRAY[0..2] OF BOOL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, iter := iter, Input := Input, ByteNum := ByteNum);
semantist_out_Out := semantist_fb.Out;
END_PROGRAM
