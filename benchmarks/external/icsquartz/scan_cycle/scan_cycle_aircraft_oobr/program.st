PROGRAM PLC_PRG
VAR_INPUT
    InRecv : ARRAY[0..7] OF LREAL;
    ssMethodType : SINT;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBR;
    semantist_out_Out : ARRAY[0..1] OF LREAL;
END_VAR
semantist_fb(InRecv := InRecv, ssMethodType := ssMethodType);
semantist_out_Out := semantist_fb.Out;
END_PROGRAM
