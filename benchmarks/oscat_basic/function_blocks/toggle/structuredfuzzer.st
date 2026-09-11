FUNCTION_BLOCK TOGGLE
VAR_INPUT
	CLK : BOOL;
	rst : BOOL;
END_VAR
VAR_OUTPUT
	Q : BOOL;
END_VAR
VAR
	edge : BOOL;
END_VAR

(*
version 1.1	30. oct. 2008
programmer 	hugo
tested by	oscat

toggle flip flop the output changes state with every rising edge of clk.

*)

(* @END_DECLARATION := '0' *)
IF rst THEN
	q := 0;
ELSIF clk AND NOT edge THEN
	Q := NOT Q;
END_IF;
edge := clk;

(* revision history

hm	13.9.2007		rev 1.0
	original version

hm	30. oct. 2008	rev 1.1
	deleted unnecessary init

*)




END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    CLK : BOOL;
    rst : BOOL;
END_VAR
VAR
    semantist_fb : TOGGLE;
    semantist_out_Q : BOOL;
END_VAR
semantist_fb(CLK := CLK, rst := rst);
semantist_out_Q := semantist_fb.Q;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
