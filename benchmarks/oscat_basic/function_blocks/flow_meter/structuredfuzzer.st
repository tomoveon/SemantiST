FUNCTION FLOOR : INT
VAR_INPUT
	X : REAL;
END_VAR


(*
version 1.1	21. mar. 2008
programmer 	hugo
tested by		tobias

This is a rounding function which returns the biggest possible integer which is less or equal to X.
floor(3.14) = 3
floor(-3.14) = -4

*)

(* @END_DECLARATION := '0' *)
FLOOR := REAL_TO_INT(X);
IF FLOOR > X THEN
	FLOOR := FLOOR - 1;
END_IF;

(* revision history
hm		7. feb 2007		rev 1.0
	originlal version

hm		21. mar. 2008	rev 1.1
	replaced trunc with real_to_int for compatibility reasons

*)
END_FUNCTION


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


FUNCTION_BLOCK FLOW_METER
VAR_INPUT
	VX : REAL;
	E : BOOL;
	RST : BOOL;

	PULSE_MODE : BOOL;
	UPDATE_TIME : TIME ;
END_VAR
VAR_OUTPUT
	F : REAL;
END_VAR
VAR_IN_OUT
	X : REAL;
	Y : UDINT;
END_VAR
VAR
	tx, tl : TIME;
	int1 : INTEGRATE;
	init: BOOL;
	e_last : BOOL;
	tmp: INT;
	x_last : REAL;
	y_last : UDINT;
END_VAR

(*
version 1.0	23. jan. 2011
programmer 	hugo
tested by	oscat

Flow meter measures flow according to gated time or pulses.

*)

(* @END_DECLARATION := '0' *)
IF NOT init THEN	(* init on power up *)
	init := TRUE;
	tl := tx;
	x_last := X;
	y_last := Y;
	int1.K := 2.7777777777777777E-4;
END_IF;

(* run integrator *)
int1(E := NOT (RST OR PULSE_MODE) AND E, X := VX, Y := X);	(* gated operation *)

IF RST THEN		(* reset *)
	X := 0.0;
	Y := 0;
	tl := tx;
	x_last := 0.0;
	y_last := 0;
ELSIF E AND PULSE_MODE THEN	(* check for pulse mode *)
	IF NOT e_last THEN X := X + VX; END_IF;
END_IF;
e_last := E;

(* reduce X to be less than 1 and increase Y respectively *)
IF X > 1.0 THEN
	tmp := FLOOR(X);
	Y := Y + INT_TO_UDINT(tmp);
	X := X - INT_TO_REAL(tmp);
END_IF;

(* calculate the current flow *)
tx := DWORD_TO_TIME(T_PLC_MS());
IF tx - tl >= UPDATE_TIME AND UPDATE_TIME > t#0s THEN
	F := (UDINT_TO_REAL(Y - y_last) + X - x_last) / TIME_TO_REAL(tx - tl) * 3.6E6;
	y_last := Y;
	x_last := X;
	tl := tx;
END_IF;



(* revision history
hm	23. jan. 2011	rev 1.0
	original version

*)

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    VX : REAL;
    E : BOOL;
    RST : BOOL;
    PULSE_MODE : BOOL;
    UPDATE_TIME : TIME;
    X : REAL;
    Y : UDINT;
END_VAR
VAR
    semantist_fb : FLOW_METER;
    semantist_out_F : REAL;
END_VAR
semantist_fb(VX := VX, E := E, RST := RST, PULSE_MODE := PULSE_MODE, UPDATE_TIME := UPDATE_TIME, X := X, Y := Y);
semantist_out_F := semantist_fb.F;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
