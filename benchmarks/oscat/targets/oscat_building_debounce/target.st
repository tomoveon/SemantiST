FUNCTION_BLOCK DEBOUNCE
VAR_INPUT
	IN : BOOL;
	TD : TIME;
	PM : BOOL;
END_VAR

VAR_OUTPUT
	Q : BOOL;
END_VAR
VAR
	deb : OSCAT_BASIC.TOF;
END_VAR

(*
version 1.0	22. sep. 2008
programmer 	oscat
tested by	tobias

DEBOUNCE is used to debounce switch inputs.
in addition to the debounce functionality this FB can also be used to detect rising edges on IN when PM = TRUE.
When PM = FALSE, Q follows in with the debounce functionality.

*)

IF NOT deb.Q AND IN THEN
	(* rising edge on input detetced a *)
	Q := TRUE;
ELSIF NOT PM THEN
	Q := deb.Q;
ELSE
	Q := FALSE;
END_IF;

(* run debounce timer *)
deb(IN := IN, PT := TD);



(*
hm 22. sep. 2008	rev 1.0
	original version

*)
END_FUNCTION_BLOCK
