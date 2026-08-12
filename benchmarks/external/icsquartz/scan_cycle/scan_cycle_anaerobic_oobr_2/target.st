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
