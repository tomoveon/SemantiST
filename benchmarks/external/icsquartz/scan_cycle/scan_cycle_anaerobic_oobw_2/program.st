PROGRAM PLC_PRG
VAR_INPUT
    In : ARRAY[0..20] OF LREAL;
    size : DWORD;
    ssMethodType : SINT;
    Out1 : LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_2;

END_VAR
semantist_fb(In := In, size := size, ssMethodType := ssMethodType, Out1 := Out1);
END_PROGRAM
