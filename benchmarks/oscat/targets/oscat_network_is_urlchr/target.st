FUNCTION IS_URLCHR : BOOL
VAR_INPUT
	IN : BYTE;
END_VAR


(*
version 1.0		27. apr. 2008
programmer 		hugo
tested by		hugo

IS_URL_CHR is true if a string contains only non reserved characters for an url.
the non reserved characters are : [A..Z], [a..z], [0..9], -_.~

*)

IS_URLCHR := ((IN > 47) AND (IN < 58)) OR ((IN > 64) AND (IN < 91)) OR ((IN > 96) AND (IN < 123)) OR (in = 126) OR (in = 95) OR (in =45) OR (in =46);


(* revision history
hm		27. apr. 2008	rev 1.0
	original version

*)
END_FUNCTION
