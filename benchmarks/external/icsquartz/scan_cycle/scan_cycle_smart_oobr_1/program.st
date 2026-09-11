PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    In_Recv : ARRAY[0..1] OF LREAL;
    In_Bit_Offset : WORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_SMART_OOBR_1;
    semantist_out_x : LREAL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, In_Recv := In_Recv, In_Bit_Offset := In_Bit_Offset);
semantist_out_x := semantist_fb.x;
END_PROGRAM
