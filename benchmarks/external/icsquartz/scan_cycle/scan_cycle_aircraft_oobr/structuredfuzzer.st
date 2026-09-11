FUNCTION MemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
MemCpy := SysMemCpy(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION SysMemCpy : REF_TO BYTE
VAR_INPUT
    pDest : REF_TO BYTE;
    pSrc : REF_TO BYTE;
    udiCount : UDINT;
END_VAR
SysMemCpy := __SEMANTIST_MEMCPY(pDest, pSrc, udiCount);
END_FUNCTION


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBR
VAR_IN_OUT
    InRecv: ARRAY[0..7] OF LREAL;
    ssMethodType: SINT;
END_VAR
VAR
	offset: DWORD;
    CycleCount: INT;
	wGust: LREAL;
    qGust: LREAL;
    iter: INT;
	In: ARRAY[0..64] OF LREAL;
END_VAR
VAR_OUTPUT
    Out: ARRAY[0..1] OF LREAL;
END_VAR


(*  Complex branching logic *)
CASE ssMethodType OF

    0:
        (*  Initialize memory *)
        FOR iter := 0 TO 64 DO
            In[iter] := 0.0;
        END_FOR;
        ssMethodType := 1;

    1:

        (*  Copy Inputs *)
        CycleCount := CycleCount + 1;
        MemCpy(REF(In), REF(InRecv), offset);
        wGust := In[0];
        qGust := In[1];

        (*  Trigger potential memory error (oob read/write) *)
        (*  printf('cycle count: %d$N', CycleCount); *)
        (*  printf('offset: %d$N', offset); *)
        IF InRecv[0] > 1.1 AND InRecv[0] < 1.5 AND InRecv[1] > 2 AND InRecv[1] < 2.14 AND InRecv[2] > 5 AND InRecv[2] < 6.5 THEN
            IF CycleCount MOD 2 = 0 THEN
                offset := offset + 10;
            END_IF
        END_IF
        (*  IF In[CycleCount MOD 64] > 10 THEN *)
        (*      In[CycleCount MOD 64 + 1] := In[CycleCount MOD 64 + 1] * 1.5; *)
        (*  ELSE *)
        (*      In[CycleCount MOD 64 + 2] := In[CycleCount MOD 64 + 2] / 0.5; *)
        (*  END_IF; *)
        
        (*  More branching *)
        IF InRecv[2] < 3.5 AND InRecv[2] > 3.01 THEN
            In[3] := In[3] - 0.7;
            IF CycleCount MOD 2 = 0 THEN
                ssMethodType := 2;
            END_IF
        ELSE
            In[4] := In[4] + 0.8;
            IF In[4] > 1.0 AND IN[4] < 10.0 THEN
                In[5] := In[5] - 0.5;
                (*  ssMethodType := 2; *)
            ELSE
                In[5] := In[5] + 0.5;
                (*  ssMethodType := 0; *)
            END_IF;
        END_IF;

        (*  Additional logic for memory error *)
        IF In[5] < -1.0 THEN
            In[6] := In[6] * 2.0;
            (*  ssMethodType := 2; *)
        ELSE
            In[7] := In[7] / 2.0;
            (*  offset := offset - 1; *)
        END_IF;

        (*  Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_AIRCRAFT_OOBR 2' *)
        Out[0] := -0.6385 * wGust;
        Out[1] := (-0.00592 * wGust) + (-0.6571 * qGust);

    2:
        (*  No action *)
END_CASE;

END_FUNCTION_BLOCK


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


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
