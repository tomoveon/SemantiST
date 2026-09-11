PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    SetpointOffsetRecv : USINT;
    MeasOffestRecv : USINT;
    iter : INT;
    SetpointRecv : ARRAY[0..ArraySize] OF LREAL;
    MeasRecv : ARRAY[0..ArraySize] OF LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBR_1;

END_VAR
semantist_fb(ssMethodType := ssMethodType, SetpointOffsetRecv := SetpointOffsetRecv, MeasOffestRecv := MeasOffestRecv, iter := iter, SetpointRecv := SetpointRecv, MeasRecv := MeasRecv);
END_PROGRAM
