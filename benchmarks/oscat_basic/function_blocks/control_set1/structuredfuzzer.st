FUNCTION_BLOCK CONTROL_SET1
VAR_INPUT
	Kt : REAL;
	Tt : REAL;
	PI : BOOL;
	PID : BOOL;

	P_K : REAL ;
	PI_K : REAL ;
	PI_TN : REAL ;
	PID_K : REAL ;
	PID_TN : REAL ;
	PID_TV : REAL ;
END_VAR
VAR_OUTPUT
	KP : REAL;
	TN : REAL;
	TV : REAL;
	KI : REAL;
	KD : REAL;
END_VAR


(*
version 1.1	11. mar. 2009
programmer 	hugo
tested by		tobias

takahashi calculates controller parameters for P, PI and PID controllers based on the ziegler nichols method.

*)

(* @END_DECLARATION := '0' *)
IF pi AND PID THEN
	KP := 0.0;
	TN := 0.0;
	TV := 0.0;
ELSIF PID THEN
	KP := PID_K * Kt;
	TN := PID_TN * Tt;
	TV := PID_TV * Tt;
ELSIF PI THEN
	KP := PI_K * Kt;
	TN := PI_TN * Tt;
ELSE
	KP := P_K * Kt;
END_IF;

(* KI and KD are calculated *)
IF tn > 0.0 THEN KI := KP / TN; ELSE KI := 0.0; END_IF;
KD := KP * TV;

(* revision history
hm	4. nov 2007	rev 1.0
	original version

hm	11. mar. 2009	rev 1.1
	real constants updated to new systax using dot																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																																											

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    Kt : REAL;
    Tt : REAL;
    PI : BOOL;
    PID : BOOL;
    P_K : REAL;
    PI_K : REAL;
    PI_TN : REAL;
    PID_K : REAL;
    PID_TN : REAL;
    PID_TV : REAL;
END_VAR
VAR
    semantist_fb : CONTROL_SET1;
    semantist_out_KP : REAL;
    semantist_out_TN : REAL;
    semantist_out_TV : REAL;
    semantist_out_KI : REAL;
    semantist_out_KD : REAL;
END_VAR
semantist_fb(Kt := Kt, Tt := Tt, PI := PI, PID := PID, P_K := P_K, PI_K := PI_K, PI_TN := PI_TN, PID_K := PID_K, PID_TN := PID_TN, PID_TV := PID_TV);
semantist_out_KP := semantist_fb.KP;
semantist_out_TN := semantist_fb.TN;
semantist_out_TV := semantist_fb.TV;
semantist_out_KI := semantist_fb.KI;
semantist_out_KD := semantist_fb.KD;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
