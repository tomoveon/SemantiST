FUNCTION_BLOCK HYST_1
VAR_INPUT
	In : REAL;
	high : REAL;
	low : REAL;
END_VAR
VAR_OUTPUT
	Q : BOOL;
	win : BOOL;
END_VAR


(*
version 1.1	2. jun. 2008
programmer 	hugo
tested BY	oscat

this hysteresis function switches the output high if the input signal reaches obove high and will switch to low when the input falls back below low value.
a separate output mid is set if the input stays between low and high value.

*)
(* @END_DECLARATION := '0' *)
IF in < low THEN
	Q := FALSE;
	win := FALSE;
ELSIF in > high THEN
	Q := TRUE;
	win := FALSE;
ELSE
	win := TRUE;
END_IF;




(* revision history
hm		4.  aug. 2006	rev 1.0
	original version

hm		2. jun. 2008	rev 1.1
	improved performance
*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    In : REAL;
    high : REAL;
    low : REAL;
END_VAR
VAR
    semantist_fb : HYST_1;
    semantist_out_Q : BOOL;
    semantist_out_win : BOOL;
END_VAR
semantist_fb(In := In, high := high, low := low);
semantist_out_Q := semantist_fb.Q;
semantist_out_win := semantist_fb.win;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
