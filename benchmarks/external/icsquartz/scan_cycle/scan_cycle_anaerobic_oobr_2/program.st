PROGRAM PLC_PRG
VAR_INPUT
    RecvInput : ARRAY[0..7] OF DINT;
    RecvOffset : DWORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_2;
    semantist_out_Out1 : LREAL;
END_VAR
semantist_fb(RecvInput := RecvInput, RecvOffset := RecvOffset);
semantist_out_Out1 := semantist_fb.Out1;
END_PROGRAM
