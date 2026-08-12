VAR_GLOBAL CONSTANT
    SS_INITIALIZE: SINT := 0;
    SS_STEP: SINT := 1;
    SS_FIN: SINT := 2;
END_VAR

FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_2
VAR_IN_OUT
    In: ARRAY[0..20] OF LREAL;
    size: DWORD;
	ssMethodType: SINT;
    Out1: LREAL;
END_VAR
VAR
    offset: DWORD;
	In_Local: ARRAY[0..20] OF LREAL;
END_VAR

CASE ssMethodType OF

    SS_INITIALIZE:
        In_Local[0] := 0.0;
        In_Local[1] := 0.0;
        ssMethodType := SS_STEP;
    
    SS_STEP:

        // Read input variable length input array into local array using swap function.
        // If the input array is longer than the local one, that will result in an out-of-bounds write.
        MemCpy(REF(In_Local[0]), REF(In[0]), offset);

        (* Outputs for Atomic SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_2 2' *)
        (* Sum: '<S1>/Sum3' *)
        Out1 := In_Local[0] + In_Local[1];
        (* Saturate: '<S1>/Sat Block (Psig)' *)
        IF Out1 >= 3.6 THEN
            Out1 := 3.6;
        ELSIF Out1 <= -8.4 THEN
            Out1 := -8.4;
            ssMethodType := SS_FIN;
        END_IF;
        (* End of Saturate: '<S1>/Sat Block (Psig)' *)
        (* Gain: '<S1>/Heat input Valve' *)
        Out1 := 0.1667 * Out1;
        (* Saturate: '<S1>/Valve Limits' *)
        IF In[0] <= -1.4 THEN
            (* Outport: '<Root>/Out1' *)
            Out1 := -1.4;
            offset := offset + 40;
        END_IF;
        (* End of Saturate: '<S1>/Valve Limits' *)
        (* End of Outputs for SubSystem: '<Root>/STFZ_SCAN_SCAN_CYCLE_ANAEROBIC_OOBW_2 2' *)

    SS_FIN:
        // Do nothing
END_CASE

END_FUNCTION_BLOCK
