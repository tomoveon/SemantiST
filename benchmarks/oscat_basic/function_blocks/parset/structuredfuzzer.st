FUNCTION T_PLC_MS : DWORD
VAR
	debug : BOOL := 0;
	N : INT := 0;
	offset : DWORD := 0;
END_VAR
VAR
	tx : TIME;
END_VAR

(*
version 1.2	16. nov 2008
programmer 	hugo
tested by	oscat

T_PLC_MS reads the internal PLC timer and return the time, it has the advantage to be able to set a debug mode 
and speed up the counter to test the plc timer overrun which occurs every 50 days respectively 25 days at siemens S7
this routine also allows to correct the behavior of s7 where the internal plc counter will not count all 32 bits.

*)
(* @END_DECLARATION := '0' *)
tx := TIME();
T_PLC_MS := TIME_TO_DWORD(Tx);
(* hier muss die korrektur f�r step7 stattfinden
plctime muss den vollen wertebereich von time ausnutzen:
wenn bei step7 time -24tage bis plus 24 tage ist dann muss der timer auch beim �berlauf auf -24tage springen 
und auf keinen fall auf 0 !!!!
f�r siemens muss ein weiterer fb im main eingebunden werden der sicherstellt das alle 32 bits durchgez�hlt werden.
es kann nur ein fb sein den er muss sich das oberste (32te) bit merken.
oder etwa spring s7 bei �berlauf auf -24 tage????? dann w�re keine korrektur n�tig.
*)
IF debug THEN
	T_PLC_MS := (SHL(T_PLC_MS,N) OR SHL(DWORD#1,N)-1) + OFFSET;
END_IF;

(* revision history
hm	14.9.2007	rev 1.0
	original version

hm	12. nov 2007	rev 1.1
	added temporaray variable tx because some compilers could not handle time() as an argument

hm	16. nov. 2008	rev 1.2
	initialized constants with 0 for compatibility reasons
*)
END_FUNCTION


FUNCTION_BLOCK PARSET
VAR_INPUT
	A0, A1 : BOOL;

	X01, X02, X03, X04 : REAL;
	X11, X12, X13, X14 : REAL;
	X21, X22, X23, X24 : REAL;
	X31, X32, X33, X34 : REAL;
	TC : TIME;
END_VAR
VAR_OUTPUT
	P1, P2, P3, P4 : REAL;
END_VAR
VAR
	X : ARRAY[0..3,1..4] OF REAL;
	S1, S2, S3, S4 : REAL;
	tx, last : DWORD;
	start : BOOL;
	set0 : BYTE;
	init: BOOL;
END_VAR

(*
version 1.1	16. mar. 2008
programmer 	hugo
tested by		tobias

parset selects on of 4 parameter sets adressed by the inputs A0 and A1. if TC is specified, the change of the outputs
is ramped by the time tc
*)

(* @END_DECLARATION := '0' *)
(* read system_time *)
tx := T_PLC_MS();

(* init sequence *)
IF NOT init THEN
	set0.0 := NOT A0;
	init := TRUE;
	X[0,1] := X01;
	X[0,2] := X02;
	X[0,3] := X03;
	X[0,4] := X04;
	X[1,1] := X11;
	X[1,2] := X12;
	X[1,3] := X13;
	X[1,4] := X14;
	X[2,1] := X21;
	X[2,2] := X22;
	X[2,3] := X23;
	X[2,4] := X24;
	X[3,1] := X31;
	X[3,2] := X32;
	X[3,3] := X33;
	X[3,4] := X34;
	P1 := X01;
	P2 := X02;
	P3 := X03;
	P4 := X04;
END_IF;

(* check for input change *)
IF (A0 XOR set0.0) OR (A1 XOR set0.1) THEN
	(* a new set is selected *)
	set0.0 := A0;
	set0.1 := A1;
	IF tc > t#0s THEN
		start := TRUE;
		last := tx;
		(* save the step speed for the output changes in S *)
		S1 := (X[set0,1] - P1)/DWORD_TO_REAL(TIME_TO_DWORD(tc));
		S2 := (X[set0,2] - P2)/DWORD_TO_REAL(TIME_TO_DWORD(tc));
		S3 := (X[set0,3] - P3)/DWORD_TO_REAL(TIME_TO_DWORD(tc));
		S4 := (X[set0,4] - P4)/DWORD_TO_REAL(TIME_TO_DWORD(tc));
	END_IF;
ELSIF start AND tx - last < TIME_TO_DWORD(tc) THEN
	(* ramp the outputs to the new value *)
	P1 := X[set0,1] - S1 * DWORD_TO_REAL(TIME_TO_DWORD(Tc) - tx + last);
	P2 := X[set0,2] - S2 * DWORD_TO_REAL(TIME_TO_DWORD(Tc) - tx + last);
	P3 := X[set0,3] - S3 * DWORD_TO_REAL(TIME_TO_DWORD(Tc) - tx + last);
	P4 := X[set0,4] - S4 * DWORD_TO_REAL(TIME_TO_DWORD(Tc) - tx + last);
ELSE
	(* make sure outputs match the correct set values *)
	start := FALSE;
	P1 := X[set0,1];
	P2 := X[set0,2];
	P3 := X[set0,3];
	P4 := X[set0,4];
END_IF;

(* revision history
hm	2.11.2007		rev 1.0
	original version

hm	16. mar. 2008	rev 1.1
	added type conversions to avoid warnings under codesys 3.0

*)

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    A0 : BOOL;
    A1 : BOOL;
    X01 : REAL;
    X02 : REAL;
    X03 : REAL;
    X04 : REAL;
    X11 : REAL;
    X12 : REAL;
    X13 : REAL;
    X14 : REAL;
    X21 : REAL;
    X22 : REAL;
    X23 : REAL;
    X24 : REAL;
    X31 : REAL;
    X32 : REAL;
    X33 : REAL;
    X34 : REAL;
    TC : TIME;
END_VAR
VAR
    semantist_fb : PARSET;
    semantist_out_P1 : REAL;
    semantist_out_P2 : REAL;
    semantist_out_P3 : REAL;
    semantist_out_P4 : REAL;
END_VAR
semantist_fb(A0 := A0, A1 := A1, X01 := X01, X02 := X02, X03 := X03, X04 := X04, X11 := X11, X12 := X12, X13 := X13, X14 := X14, X21 := X21, X22 := X22, X23 := X23, X24 := X24, X31 := X31, X32 := X32, X33 := X33, X34 := X34, TC := TC);
semantist_out_P1 := semantist_fb.P1;
semantist_out_P2 := semantist_fb.P2;
semantist_out_P3 := semantist_fb.P3;
semantist_out_P4 := semantist_fb.P4;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
