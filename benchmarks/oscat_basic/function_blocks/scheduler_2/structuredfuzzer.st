FUNCTION_BLOCK SCHEDULER_2
VAR_INPUT
	E0 : BOOL;
	E1 : BOOL;
	E2 : BOOL;
	E3 : BOOL;

	C0, C1, C2, C3 : UINT;
	O0, O1, O2, O3 : UINT;
END_VAR
VAR_OUTPUT
	Q0, Q1, Q2, Q3 : BOOL;
END_VAR
VAR
	sx : UINT;
END_VAR


(*
version 1.0	29. sep 2008
programmer 	hugo
tested by		tobias

SCHEDULER_2 is used to call programs or function blocks at specific cycles.
C0..C3 defines after how many cycles the output becomes active.
O0..O3 defines a cycle offset at startup.

*)

(* @END_DECLARATION := '0' *)
Q0 := E0 AND (sx MOD C0 - O0 = 0);
Q1 := E1 AND (sx MOD C1 - O1 = 0);
Q2 := E2 AND (sx MOD C2 - O2 = 0);
Q3 := E3 AND (sx MOD C3 - O3 = 0);

(* increment cycle counter every cycle *)
sx := sx + 1;


(* revision history
hm 29. sep. 2008		rev 1.0
	original version


*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    E0 : BOOL;
    E1 : BOOL;
    E2 : BOOL;
    E3 : BOOL;
    C0 : UINT;
    C1 : UINT;
    C2 : UINT;
    C3 : UINT;
    O0 : UINT;
    O1 : UINT;
    O2 : UINT;
    O3 : UINT;
END_VAR
VAR
    semantist_fb : SCHEDULER_2;
    semantist_out_Q0 : BOOL;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
    semantist_out_Q3 : BOOL;
END_VAR
semantist_fb(E0 := E0, E1 := E1, E2 := E2, E3 := E3, C0 := C0, C1 := C1, C2 := C2, C3 := C3, O0 := O0, O1 := O1, O2 := O2, O3 := O3);
semantist_out_Q0 := semantist_fb.Q0;
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
semantist_out_Q3 := semantist_fb.Q3;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
