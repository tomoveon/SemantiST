FUNCTION_BLOCK TREND
VAR_INPUT
	X : REAL;
END_VAR
VAR_OUTPUT
	Q : BOOL;
	TU : BOOL;
	TD : BOOL;
	D : REAL;
END_VAR
VAR
	last_X: REAL;
END_VAR

(*
version 1.0	21. aug. 2009
programmer 	hugo
tested by		oscat

trend analyses the trend of a input signal.
The Output Q is True if the input X is >= last_X and is false if the input X is <= last_X
in addition to the trend the output TU will be high for one cycle to signal a rising of the input value X
and  TD will signal a decreasing value on the input X.
in case of a change the output D will show the delta of the input to the last input value.

*)
(* @END_DECLARATION := '0' *)
TU := X > last_X;
TD := X < last_X;
Q := TU OR TD;
D := X - LAST_X;

last_X := X;



(* revision history
hm	21. aug. 2009	rev 1.0
	original version

*)

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
END_VAR
VAR
    semantist_fb : TREND;
    semantist_out_Q : BOOL;
    semantist_out_TU : BOOL;
    semantist_out_TD : BOOL;
    semantist_out_D : REAL;
END_VAR
semantist_fb(X := X);
semantist_out_Q := semantist_fb.Q;
semantist_out_TU := semantist_fb.TU;
semantist_out_TD := semantist_fb.TD;
semantist_out_D := semantist_fb.D;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
