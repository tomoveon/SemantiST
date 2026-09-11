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


FUNCTION R2_ADD : REAL2
VAR_INPUT
	X : Real2;
	Y : REAL;
END_VAR
VAR
	temp: REAL;
END_VAR

(*
version 1.0	20. mar. 2008
programmer 	hugo
tested by	tobias

R2_add adds a real to a double real which extends the accuracy of a real to twice as many digits

*)
(* @END_DECLARATION := '0' *)
temp := X.RX;
R2_ADD.RX := Y + X.R1 + X.RX;
R2_ADD.R1 := temp - R2_ADD.RX + Y + X.R1;


(* revision history
hm		20.3.2008	rev 1.0
	original version

*)
END_FUNCTION


FUNCTION_BLOCK METER
VAR_INPUT
	M1, M2 : REAL;
	I1, I2 : BOOL;
	D : REAL ;
	RST : BOOL;
END_VAR
VAR_IN_OUT
	MX : REAL;
END_VAR
VAR
	MR : REAL2;
	MX1, MX2 : REAL; (* current consumption value on M1 and M2 *)
	tx: DWORD;
	last: DWORD;
	tc: REAL;
	init: BOOL;
END_VAR

(*
	version 1.4	8. mar. 2009
	programmer 	oscat
	tested BY	oscat

METER measures usage of power or similar values the output MX is the sum of the inputs over time.
MX := sum over time of (I1 * P1 + I2 * P2)/D.
a simple example is the consumption counter for a 2 stage oil burner where stage 1 is 10KW and stage 2 is 20KW.
I1 and I2 decide which value is accounted for, I1 = True only counts P1, I2 is True counts P2 
and I1 and I2 is true counts P1 and P2.
the meter can be used for all kind of consumption meters. P1 and P2 can change on the fly.

*)
(* @END_DECLARATION := '0' *)
(* measure the last cycle time and make sure we onle execute once every millisecond *)
tx := T_PLC_MS();
IF NOT init THEN
	init := TRUE;
	last := tx;
	mr.RX := mx;
	mr.R1 := 0.0;
ELSIF tx = last THEN
	RETURN;
ELSE
	tc := DWORD_TO_REAL(tx - last) * 0.001;
END_IF;
last := tx;

(* reset *)
IF rst THEN
	mr.R1 := 0.0;
	mr.RX := 0.0;
ELSE
	(* current consumption measurement *)
	IF I1 THEN MX1 := M1; ELSE MX1 := 0.0; END_IF;
	IF I2 THEN MX2 := M2; ELSE MX2 := 0.0; END_IF;
	(* add up the current values in a double real *)
	MR := R2_ADD(MR,(SEL(I1,0.0,mx1) + SEL(I2, 0.0, mx2)) / D * TC);
	(* set the current output value *)
	MX := mr.RX;
END_IF;




(* revision history

hm	16. sep.2007		rev 1.0
	original version

hm	7. feb 2008		rev 1.1
	use new version of ft_int to avoid a counting stop at high values
	deleted unnecessary limits

hm	24. mar. 2008	rev 1.2
	use data_type real2 to extend accuracy to 15 digits total
	do not use ft_int which adds unnecessary overhead

hm	8. feb. 2009	rev 1.3
	changed mx to be I/O
	make sure calculation works for cycle times < 1 ms

hm	8. mar. 2009	rev 1.4
	last was not updated
	code improvements

*)

END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    M1 : REAL;
    M2 : REAL;
    I1 : BOOL;
    I2 : BOOL;
    D : REAL;
    RST : BOOL;
    MX : REAL;
END_VAR
VAR
    semantist_fb : METER;

END_VAR
semantist_fb(M1 := M1, M2 := M2, I1 := I1, I2 := I2, D := D, RST := RST, MX := MX);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
