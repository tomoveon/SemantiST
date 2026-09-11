FUNCTION_BLOCK INTERLOCK
VAR_INPUT
	I1, I2 : BOOL;
	TL : TIME;
END_VAR
VAR_OUTPUT
	Q1, Q2 : BOOL;
END_VAR
VAR
	T1, T2 : TOF;
END_VAR

(*
version 1.0	28 sep 2007
programmer 	hugo
tested by		tobias

INTERLOCK has two inputs I1 and I2 which drive the corresponding outputs Q1 and Q2.
the inputs signals lock each other out and therfore I1 can only drive Q1 when I2 is Low and vice versa.
The input TL specifies a dead time between two outputs can become active.
an output can only become active when the other output was not active for the time TL.

*)
(* @END_DECLARATION := '0' *)
(* the input signal have a run delay to lockout the other input *)
T1(IN := I1, PT := TL);
T2(IN := I2, PT := TL);

Q1 := I1 AND NOT t2.Q;
Q2 := I2 AND NOT t1.Q;


(* revision history

hm	28 sep 2007		rev 1.0
	original version

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    I1 : BOOL;
    I2 : BOOL;
    TL : TIME;
END_VAR
VAR
    semantist_fb : INTERLOCK;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
END_VAR
semantist_fb(I1 := I1, I2 := I2, TL := TL);
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
