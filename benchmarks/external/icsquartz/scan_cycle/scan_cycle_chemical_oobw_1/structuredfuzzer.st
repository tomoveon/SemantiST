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


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBW_1
VAR_IN_OUT
    ssMethodType: SINT;
    iter: INT;
    size: INT;
    In1_Arg: ARRAY [0..40] OF LREAL;
    In2_Arg: ARRAY [0..40] OF LREAL;

    (*  In1_Arg: POINTER TO ARRAY [0..20] OF LREAL; *)
    (*  In2_Arg: POINTER TO ARRAY [0..20] OF LREAL; *)
    perhr: LREAL;
END_VAR
VAR
	In1: ARRAY [0..40] OF LREAL;
    In2: ARRAY [0..40] OF LREAL;

    Gain2_Gain: ARRAY [0..6] OF LREAL := [2.209,6.177,22.06,14.56,17.89,30.44,22.94];

    In1_0: ARRAY [0..6] OF LREAL;
    tmp: LREAL;
    i: DINT;
END_VAR


(*  Copy Input Arrays. Because we use dynamic arrays and get their upper bound on runtime, *)
(*  if the caller passes arrays with a larger size than the preallocated local arrays, this will result in an Out-of-Bounds write. *)
MemCpy(REF(In1[0]), REF(In1_Arg[0]), size * SIZEOF(LREAL));
MemCpy(REF(In2[0]), REF(In2_Arg[0]), size * SIZEOF(LREAL));

CASE ssMethodType OF

    0:
        In1[0] := 0.0;
        ssMethodType := 1;

    1:
        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBW_1 2' *)
        (* SignalConversion: '<S2>/TmpSignal ConversionAtGain2Inport1' *)
        In1_0[4] := In1[28];
        FOR i := 0 TO 5 DO
            In1_0[i + 1] := In1[i + 30];
        END_FOR;
        (* End of SignalConversion: '<S2>/TmpSignal ConversionAtGain2Inport1' *)
        (* Gain: '<S2>/Gain2' *)
        tmp := 0.0;
        FOR i := 0 TO 6 DO
            tmp := (Gain2_Gain[i] * In1_0[i]) + tmp;
        END_FOR;

        iter := iter + 1;
        (*  size := size + 1; *)
        (*  printf('size: %d$N', size); *)
        (*  print in1_arg0 *)
        (*  printf('In1_Arg[0]: %f$N', In1_Arg[0]); *)
        IF In1_Arg[0] > 0.14 AND In1_arg[0] < 20.13 THEN
            size := size + 100;
        END_IF

        (* Outport: '<Root>/$ per hr' incorporates:
        *  Gain: '<S2>/Gain'
        *  Gain: '<S2>/Gain1'
        *  Gain: '<S2>/Gain2'
        *  Gain: '<S2>/Gain3'
        *  Gain: '<S2>/Gain4'
        *  Product: '<S2>/Product'
        *  Product: '<S2>/Product1'
        *  Sum: '<S2>/Sum' *)
        perhr := ((((0.2206 * In1[36]) + (0.1456 * In1[37])) + (0.1789 * In1[38])) * (4.541 * In2[7])) + (((0.0318 * In1[18]) + (0.0536 * In1[19])) + ((0.44791 * In1[9]) * tmp));
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBW_1 2' *)

        If In1_Arg[0] <= 3.14 AND In1_Arg[0] > 0 THEN
            ssMethodType := 2;
        END_IF

    2:
        (*  Do nothing *)
END_CASE

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    iter : INT;
    size : INT;
    In1_Arg : ARRAY[0..40] OF LREAL;
    In2_Arg : ARRAY[0..40] OF LREAL;
    perhr : LREAL;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_CHEMICAL_OOBW_1;

END_VAR
semantist_fb(ssMethodType := ssMethodType, iter := iter, size := size, In1_Arg := In1_Arg, In2_Arg := In2_Arg, perhr := perhr);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
