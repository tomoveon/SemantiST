FUNCTION EXP10 : REAL
VAR_INPUT
	X : REAL;
END_VAR


(*
version 1.0	 2 dec 2007
programmer 	hugo
tested by		tobias

this function calculates the exponent to the basis 10
exp10(3) = 1000

*)
(* @END_DECLARATION := '0' *)
EXP10 := EXP(X * 2.30258509299405);

(* revision history
hm	2. dec 2007		rev 1.0
	original version

*)
END_FUNCTION


FUNCTION REAL_TO_STRF : STRING
VAR_INPUT
	IN : REAL;
	N : INT;
	D : STRING;
END_VAR
VAR
	O: REAL;
	i: INT;
END_VAR

(*
version 1.8	2. jan 2012
programmer 	hugo
tested by	oscat

Real_to_strf converts a Real to a fixed length String.
the string will be filles with zeroes to achieve the fixed length N after the decimal separator.
the input parameter specifies the decimal separator to be used in the output string.

*)


(* @END_DECLARATION := '0' *)
(* LIMIT N to 0 .. 7 *)
N := LIMIT(0,N,7);
(* round the input to N digits and convert to string *)
O := ABS(in) * EXP10(N);
REAL_TO_STRF := DINT_TO_STRING(REAL_TO_DINT(O));
(* add zeroes in front to make sure sting is at least 8 digits long *)
FOR i := LEN(REAL_TO_STRF) TO N DO REAL_TO_STRF := CONCAT('0', REAL_TO_STRF); END_FOR;
(* add a dot if n > 0 *)
IF n > 0 THEN REAL_TO_STRF := INSERT(REAL_TO_STRF, D, LEN(REAL_TO_STRF) - N); END_IF;
(* add a minus sign if in is negative *)
IF in < 0 THEN REAL_TO_STRF := CONCAT('-', REAL_TO_STRF); END_IF;


(* revision history
hm	26 jan 2007	rev 1.0
	original version

hm	20. nov. 2007	rev 1.1
	when N=0 ther will be no dot at the end of the string.

hm	15. dec. 2007	rev 1.2
	changed code for better performance

hm	4. mar. 2008	rev 1.3
	result is now rounded instead of trunc

hm	20. mar. 2008	rev 1.4
	changed trunc to real_to_dint because trunc was generating wrong values on wago 842

hm	29. mar. 2008	rev 1.5
	changed STRING to STRING

hm	4. apr. 2008	rev 1.6
	added variable O to avoid an error uner CoDeSys SP PLCWinNT V2.4

hm	27. feb. 2009	rev 1.7
	added a missing zero for IN < 1

hm 2. jan 2012	rev 1.8
	added input parameter D to specify decimal separator
*)

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    IN : REAL;
    N : INT;
    D : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := REAL_TO_STRF(IN := IN, N := N, D := D);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
