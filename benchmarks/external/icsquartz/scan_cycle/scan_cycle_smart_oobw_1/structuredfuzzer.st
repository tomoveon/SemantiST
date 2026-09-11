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


FUNCTION_BLOCK STFZ_SCAN_SCAN_CYCLE_SMART_OOBW_1
VAR_IN_OUT
    ssMethodType: SINT;
    iter: INT;
    Input: POINTER TO LREAL;
	ByteNum: DWORD;
END_VAR
VAR_OUTPUT
    Out: ARRAY [0..2] OF BOOL;
    (* Out2: BOOL; *)
END_VAR
VAR
    rtb_Switch: LREAL;
    rtb_RelationalOperator: BOOL;
	In: ARRAY[0..6] OF LREAL;
END_VAR

(* Outputs for Atomic SubSystem: '<Root>/Code_1' *)
(* Switch: '<S1>/Switch' incorporates:
 *  Gain: '<S1>/Gain2'
 *  Product: '<S1>/Product'
 *  Sum: '<S1>/Sum'
 *  Trigonometry: '<S1>/Trigonometric Function' *)

CASE ssMethodType OF

    0:
        iter := 0;
        ByteNum := 16;
        ssMethodType := 1;

    1:
        (*  Increment *)
        iter := iter + 1;

        (*  Copy Input to In *)
        MemCpy(REF(In[0]), Input, ByteNum);

        IF In[4] >= 0.5 THEN
            rtb_Switch := SIN(((0.017453292519943295 * In[2]) + In[1]) + In[4]) * In[0];
        ELSE
            rtb_Switch := In[5];
        END_IF;

        IF In[0] > 4 AND In[0] < 4.13 THEN
            ByteNum:= ByteNum + 12;
        END_IF

        (* End of Switch: '<S1>/Switch' *)
        (* RelationalOperator: '<S1>/Relational Operator' *)
        rtb_RelationalOperator := rtb_Switch >= In[6];
        (* End of Outputs for SubSystem: '<Root>/Code_1' *)
        (* Outport: '<Root>/Out1' incorporates:
        *  Logic: '<S1>/BL4' *)
        Out[0] := rtb_RelationalOperator;
        (* Outputs for Atomic SubSystem: '<Root>/Code_1' *)
        Out[1] :=  NOT rtb_RelationalOperator;
        (* Outport: '<Root>/Out2' incorporates:
        *  Gain: '<S1>/Gain3'
        *  RelationalOperator: '<S1>/Relational Operator1' *)
        Out[2] := ( -rtb_Switch) >= In[6];
        (* End of Outputs for SubSystem: '<Root>/Code_1' *)

        If In[0] <= 3.14 AND In[0] > 0.5 THEN
            ssMethodType := 2;
        END_IF

    2:
        (*  Done *)

END_CASE

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ssMethodType : SINT;
    iter : INT;
    Input : POINTER TO LREAL;
    ByteNum : DWORD;
END_VAR
VAR
    semantist_fb : STFZ_SCAN_SCAN_CYCLE_SMART_OOBW_1;
    semantist_out_Out : ARRAY[0..2] OF BOOL;
END_VAR
semantist_fb(ssMethodType := ssMethodType, iter := iter, Input := Input, ByteNum := ByteNum);
semantist_out_Out := semantist_fb.Out;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
