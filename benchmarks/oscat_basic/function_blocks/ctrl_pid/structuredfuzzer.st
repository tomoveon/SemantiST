FUNCTION MANUAL : BOOL
VAR_INPUT
	IN : BOOL;
	ON : BOOL;
	OFF : BOOL;
END_VAR


(*
version 1.0	21. nov. 2008
programmer 	hugo
tested by	oscat

MANUAL is a manual override for digital signals.
when on and off = FALSE, the output follows IN.
ON = TRUE forces the output high, and OFF = TRUE forces the output low.


*)
(* @END_DECLARATION := '0' *)
MANUAL := NOT OFF AND (IN OR ON);



(* revision history
hm	21. nov 2008	rev 1.0
	original version


*)
END_FUNCTION


FUNCTION OFFSET : REAL
VAR_INPUT
	X : REAL;
	O1, O2, O3, O4 : BOOL;
	D : BOOL;

	Offset_1 : REAL;
	Offset_2 : REAL;
	Offset_3 : REAL;
	Offset_4 : REAL;
	default : REAL;
END_VAR

(*
version 1.0	12 jan 2007
programmer 	oscat
tested by	oscat

The Function offset adds offsets to an analog signal depending on digital inputs.
all selected offsets are added at the same time.
with the input D a default value instead of the input X can be used.

*) 
(* @END_DECLARATION := '0' *)
IF D THEN OFFSET := default; ELSE OFFSET := X; END_IF;
IF O1 THEN OFFSET := OFFSET + offset_1; END_IF;
IF O2 THEN OFFSET := OFFSET + offset_2; END_IF;
IF O3 THEN OFFSET := OFFSET + offset_3; END_IF;
IF O4 THEN OFFSET := OFFSET + offset_4; END_IF;



(* revision history
hm	12. jan 2007
	original version


*)
END_FUNCTION


FUNCTION CTRL_IN : REAL
VAR_INPUT
	SET_POINT, ACTUAL, NOISE : REAL;
END_VAR


(*
version 1.0	2. jun 2008
programmer 	hugo
tested by		tobias


*)

(* @END_DECLARATION := '0' *)
(* calculate the process error DIFF *)
CTRL_IN := DEAD_ZONE(SET_POINT - ACTUAL, NOISE);


(* revision history
hm 	2. jun. 2008 	rev 1.0
	original version

*)
END_FUNCTION


FUNCTION DEAD_ZONE : REAL
VAR_INPUT
	X : REAL;
	L : REAL;
END_VAR


(*
version 1.2	11. mar. 2009
programmer 	hugo
tested by		oscat

dead_zone2 is a linear transfer function which follows a linear function except for x is close to 0.
Y = X if abs(x) > L otherwise its 0.

*)
(* @END_DECLARATION := '0' *)
IF ABS(x) > L THEN
	dead_zone := X;
ELSE
	DEAD_ZONE := 0.0;
END_IF;

(* revision history
hm	12. feb. 2007	rev 1.0
	original version

hm	14. jun. 2008	rev 1.1
	improved performance

hm	11. mar. 2009	rev 1.2
	real constants updated to new systax using dot

*)
END_FUNCTION


FUNCTION_BLOCK CTRL_PID
VAR_INPUT
	ACT, SET0, SUP, OFS, M_I : REAL;
	MAN : BOOL;
	RST : BOOL;
	KP : REAL ;
	TN : REAL ;
	TV : REAL ;
	LL : REAL ;
	LH : REAL ;
END_VAR
VAR_OUTPUT
	Y : REAL;
	DIFF : REAL;
	LIM : BOOL;
END_VAR
VAR
	pid : FT_PIDWL;
	co : CTRL_OUT;
END_VAR



(*
version 2.0	30. jun. 2008
programmer 	hugo
tested by	oscat

FT_PI is a PI controller with manual functionality.
The PID controller works according to the fomula Y = e *(KP+ KI * INTEG(e) ) + offset, while e = set_point - actual.
a rst will reset all internal data, while a switch to manual will cause the controller to follow the function Y = manual_in + offset.
limit_h and Limit_l set the possible output range of Y.
the output flags lim will signal that the output limits are active.

*)

(* @END_DECLARATION := '0' *)
DIFF := CTRL_IN(SET0, ACT, SUP);
pid(in := DIFF, kp := KP, tn := TN, tv := TV, lim_l := LL, lim_h := LH, rst := RST);
co(ci := pid.Y, OFFSET := OFS, man_in := M_I, lim_l := LL, lim_h := LH, MANUAL := MAN);
Y := co.Y;
LIM := co.LIM;



(* revision history

hm 1.12.2006	 	rev 1.1
	changed algorithm to trapezregel for higher accuracy.

hm 3.1.2007		rev 1.2
	added integ_band to select when the integrator is active.

hm	3.3.2007		rev 1.3
	added default values to inputs KP, TN, TV, LIMIT_L und LIMIT_H.

hm 31.oct 2007	rev 1.4
	total rewrite of the module to avoid failures when one of the limits is 0

hm	3.11.2007	rev 1.5
	added noise input to filter noise
	added output diff
	set limit output false when output is within limits
	overfolw was not set correctly

hm	5. jan 2008	rev 1.6
	improved code for better performance
*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    ACT : REAL;
    SET0 : REAL;
    SUP : REAL;
    OFS : REAL;
    M_I : REAL;
    MAN : BOOL;
    RST : BOOL;
    KP : REAL;
    TN : REAL;
    TV : REAL;
    LL : REAL;
    LH : REAL;
END_VAR
VAR
    semantist_fb : CTRL_PID;
    semantist_out_Y : REAL;
    semantist_out_DIFF : REAL;
    semantist_out_LIM : BOOL;
END_VAR
semantist_fb(ACT := ACT, SET0 := SET0, SUP := SUP, OFS := OFS, M_I := M_I, MAN := MAN, RST := RST, KP := KP, TN := TN, TV := TV, LL := LL, LH := LH);
semantist_out_Y := semantist_fb.Y;
semantist_out_DIFF := semantist_fb.DIFF;
semantist_out_LIM := semantist_fb.LIM;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
