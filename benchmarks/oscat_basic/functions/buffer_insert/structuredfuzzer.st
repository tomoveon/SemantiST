FUNCTION _STRING_TO_BUFFER : INT
VAR_INPUT
	STR : STRING;
	POS : INT;
	PT : REF_TO ARRAY[0..32767] OF BYTE;
	SIZE : UINT;
END_VAR
VAR
	ps : REF_TO BYTE;
	i: INT;
	end: INT;
END_VAR


(*
version 1.4	2. jan. 2012
programmer 	hugo
tested by		oscat

this function will copy a string into an array of byte starting at position pos.
the function needs to be called:	_String_To_buffer(str, pos, adr("array"),sizeof("array"));
this function will manipulate the array directly in memory and returns the position after the input string when finished.
because this function works with pointers its very time efficient and it needs no extra memory.

*)
(* @END_DECLARATION := '0' *)
ps := REF(str);
end := MIN(pos + LEN(str), UINT_TO_INT(size));
IF end > 0 THEN end := end -1; END_IF;
FOR i := pos TO end DO
	pt^[i] := ps^;
	ps := ps + 1;
END_FOR;

_STRING_TO_BUFFER := i;

(* revision History

hm 	5. mar. 2008	rev 1.0
	original version

hm	16. mar. 2008	rev 1.1
	changed type of input size to uint

hm	13. may. 2008	rev 1.2
	changed type of pointer to array[1..32767]
	changed size of string to 250

hm	12. nov. 2009	rev 1.3
	limit end to size - 1

hm	2. jan 2012	rev 1.4
	return the position after the input string when finished
*)
END_FUNCTION


FUNCTION _BUFFER_INSERT : INT
VAR_INPUT
	STR : STRING;
	POS : INT;
	PT : REF_TO ARRAY[0..32767] OF BYTE;
	SIZE : UINT;
END_VAR
VAR
	end : INT;
	lx: INT;
	i: INT;
END_VAR


(*
version 1.5	2. jan. 2012
programmer 	hugo
tested by	oscat

this function will insert a string at a given position in a buffer.
the function needs to be called:	_buffer_init(adr("array"),sizeof("array"),init);
this function will manipulate a given array.
the function manipulates the original array, it returnes the next position after the input string when finished.
because this function works with pointers its very time efficient and it needs no extra memory.

*)
(* @END_DECLARATION := '0' *)
lx := LEN(str);
end := pos + lx;
(* first move the upper part of the buffer to make space for the string *)
FOR i := UINT_TO_INT(size) - 1 TO end BY -1 DO
	pt^[i] := pt^[i-lx];
END_FOR;
(* copy the string *)
_BUFFER_INSERT := _STRING_TO_BUFFER(str, pos, PT, size);


(* revision History
hm 	9. mar. 2008	rev 1.0
	original version

hm	16. mar. 2008	rev 1.1
	changed type of input size to uint

hm	13. may. 2008	rev 1.2
	changed type of pointer to array[1..32767]
	changed size of string to 250

hm	23. mar. 2009	rev 1.3
	avoid writing to input pos

hm	12. nov. 2009	rev 1.4
	code optimized

hm 2. jan 2012	rev 1.5
	function now returns the next position after the input string

*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    STR : STRING;
    POS : INT;
    PT : REF_TO ARRAY[0..32767] OF BYTE;
    SIZE : UINT;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := _BUFFER_INSERT(STR := STR, POS := POS, PT := PT, SIZE := SIZE);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
