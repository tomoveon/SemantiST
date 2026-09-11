FUNCTION_BLOCK CALIBRATE
VAR_INPUT
	X : REAL;
	CO : BOOL;
	CS : BOOL;

	Y_Offset : REAL;
	Y_Scale : REAL;
END_VAR
VAR_OUTPUT
	Y : REAL;
END_VAR
VAR
	offset : REAL;
	Scale : REAL := 1.0;
END_VAR

(*
	version 1.3	11. mar. 2009
	programmer 	hugo
	tested BY		hans

Calibrate allows for offset and scale calibration of an analog input.
offset is calibrated to a stored reference Y_offset while CO is true.
after the offset is calibrated, scale can be calibrated to a reference value Y_scale while CS is true.
*)
(* @END_DECLARATION := '0' *)
(* check for calibration *)
IF CO THEN
	OFFSET := Y_Offset - X;
ELSIF CS THEN
	SCALE := Y_scale / (X + OFFSET);
END_IF;
(* calculate output *)
Y := (X + OFFSET) * SCALE;


(* revision history
hm 22.2.2007		rev 1.2
	changed VAR PERSISTENT to VAR for better compatibility
	wago lon contollers do not support persisitent

hm	11. mar. 2009	rev 1.3
	changed real constants to use dot syntax

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
    CO : BOOL;
    CS : BOOL;
    Y_Offset : REAL;
    Y_Scale : REAL;
END_VAR
VAR
    semantist_fb : CALIBRATE;
    semantist_out_Y : REAL;
END_VAR
semantist_fb(X := X, CO := CO, CS := CS, Y_Offset := Y_Offset, Y_Scale := Y_Scale);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
