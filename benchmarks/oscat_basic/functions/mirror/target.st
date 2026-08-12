FUNCTION MIRROR : STRING[STRING_LENGTH]
VAR_INPUT
	str : STRING[STRING_LENGTH];
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
	changed STRING to STRING[STRING_LENGTH]
*)	

END_FUNCTION
