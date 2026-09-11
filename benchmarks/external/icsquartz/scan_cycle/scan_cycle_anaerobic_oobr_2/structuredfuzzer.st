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


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_2
VAR_IN_OUT
    RecvInput: ARRAY[0..7] OF DINT;
	RecvOffset: DWORD;
END_VAR
VAR
	Input: ARRAY[0..255] OF LREAL;
	In1: LREAL;
    In2: LREAL;
    iter: DINT;
    DONE: DINT;
END_VAR
VAR_OUTPUT
    Out1: LREAL;
END_VAR

IF DONE THEN
    RETURN
END_IF

IF RecvInput[4] > 4 AND RecvInput[4] < 8 THEN
    DONE := 1;
END_IF

IF RecvInput[0] > 1 AND RecvInput[0] < 8 AND RecvInput[4] < -10 THEN
    RecvOffset:= RecvOffset + 16;
END_IF


iter := iter + 1;

MemCpy(REF(Input), REF(RecvInput), RecvOffset);
In1:= Input[0];
In2:= Input[1];

(* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_2 2' *)
(* Sum: '<S1>/Sum3' *)
Out1 := In1 + In2;
(* Saturate: '<S1>/Sat Block (Psig)' *)
IF Out1 >= 3.6 THEN
    Out1 := 3.6;
ELSIF Out1 <= -8.4 THEN
    Out1 := -8.4;
END_IF;
(* End of Saturate: '<S1>/Sat Block (Psig)' *)
(* Gain: '<S1>/Heat input Valve' *)
Out1 := 0.1667 * Out1;
(* Saturate: '<S1>/Valve Limits' *)
IF Out1 <= -1.4 THEN
    (* Outport: '<Root>/Out1' *)
    Out1 := -1.4;
END_IF;
(* End of Saturate: '<S1>/Valve Limits' *)
(* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBR_2 2' *)

END_FUNCTION_BLOCK


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


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
