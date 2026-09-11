FUNCTION INC1 : INT
VAR_INPUT
	X : INT;
	N : INT;
END_VAR



(*
version 1.2	23. feb. 2009
programmer 	hugo
tested by	oscat

This is a increment function which increments the variable X by 1 and if N is reached, it begins with 0 instead of N again.
inc1(X,3) will generate 0,1,2,0,1,.....

*)
(* @END_DECLARATION := '0' *)
IF X >= N - 1 THEN
	INC1 := 0;
ELSE
	INC1 := X + 1;
END_IF;



(* revision history
hm	13. oct. 2008	rev 1.0
	original version

hm	22. oct. 2008	rev 1.1
	added statement to return value for compatibility reasons

hm	23. feb. 2009	rev 1.2
 	when inc1 is called with X >= N inc will continue with 0
*)
END_FUNCTION


FUNCTION_BLOCK FILTER_MAV_DW
VAR_INPUT
	X : DWORD;
	N : UINT;
	RST : BOOL;
END_VAR
VAR_OUTPUT
	Y : DWORD;
END_VAR
VAR
	init: BOOL;
	buffer : ARRAY[0..31] OF DWORD;
	i: INT;
END_VAR
VAR_TEMP
	tmp: INT;
END_VAR

(*
version 1.3	23. feb. 2009
programmer 	hugo
tested by		oscat

FILTER_MAV_DW is a moving average filter with programmable length N for DWORD Data.
 
*)
(* @END_DECLARATION := '0' *)
(* limit N to size of buffer *)
N := MIN(N, 32);

(* startup initialisation *)
IF NOT init OR rst OR N = 0 THEN
	init := TRUE;
	tmp := UINT_TO_INT(N)-1;
	FOR i := 0 TO tmp DO
		buffer[i] := X;
	END_FOR;
	Y := X;
ELSE
	tmp := UINT_TO_INT(N);
	i := INC1(i, tmp);
	Y := Y + (X - buffer[i]) / N;
	buffer[i] := X;
END_IF;




(*
hm 13. oct. 2008	rev 1.0
	original version

hm	27. oct. 2008	rev 1.1
	added typecast to avoid warnings

hm	24. nov. 2008	rev 1.2
	added typecasts to avoid warnings
	avoid divide by 0 if N = 0

hm	23. feb. 2009	rev 1.3
	limit N to max array size

*)
END_FUNCTION_BLOCK


PROGRAM PLC_PRG
VAR_INPUT
    X : DWORD;
    N : UINT;
    RST : BOOL;
END_VAR
VAR
    semantist_fb : FILTER_MAV_DW;
    semantist_out_Y : DWORD;
END_VAR
semantist_fb(X := X, N := N, RST := RST);
semantist_out_Y := semantist_fb.Y;
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
