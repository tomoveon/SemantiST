FUNCTION MIRROR : STRING
VAR_INPUT
	str : STRING;
END_VAR
VAR
	pi : REF_TO ARRAY[1..255] OF BYTE;
	po : REF_TO BYTE;
	lx: INT;
	i: INT;
END_VAR

(*
version 1.1	29. mar. 2008
programmer 	hugo
tested by	tobias

this function reverses an input string.

*)
(* @END_DECLARATION := '0' *)
pi := REF(str);
po := REF(mirror);
lx := LEN(str);
FOR i := lx TO 1 BY - 1 DO
	po^ := pi^[i];
	po := po + 1;
END_FOR;
(* close output string *)
po^:= 0;


(* revision histroy
hm	4. feb. 2008	rev 1.0
	original release

hm	29. mar. 2008	rev 1.1
	changed STRING to STRING
*)	

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := MIRROR(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
