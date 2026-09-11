PROGRAM PLC_PRG
VAR_INPUT
    DTI : DT;
    MS : INT;
    FMT : STRING;
    LANG : INT;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := DT_TO_STRF(DTI := DTI, MS := MS, FMT := FMT, LANG := LANG);
END_PROGRAM
