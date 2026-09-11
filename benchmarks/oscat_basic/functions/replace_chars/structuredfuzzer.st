FUNCTION REPLACE_CHARS : STRING
VAR_INPUT
	STR : STRING;
	SRC : STRING;
	REP : STRING;
END_VAR
VAR
	a, b : INT;
	c: STRING;
	stp: INT;
END_VAR

(*
version 1.0		14. may. 2008
programmer 		hugo
tested by			hugo

REPLACE_CHARS erstezt alle character die im string src aufgef�hrt sind mit dem an der selben stelle im string rep gelisteten character.

*)
(* @END_DECLARATION := '0' *)
REPLACE_CHARS := STR;
(* make sure rep and src are of same length and length is > 0 *)
a := LEN(src);
b := LEN(rep);
IF a < b THEN
	rep := LEFT(rep, a);
ELSIF b < a THEN
	src := LEFT(src, b);
END_IF;

(* search the string and replace if necessary *)
stp := LEN(str);
FOR a := 1 TO stp DO
	c := MID(REPLACE_CHARS, 1, a);
	b := FIND(src, c);
	IF b > 0 THEN
		REPLACE_CHARS := REPLACE(REPLACE_CHARS, MID(rep, 1, b), 1, a);
	END_IF;
END_FOR;



(* revision history
hm	14. may. 2008	rev 1.0
	original version

*)

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    STR : STRING;
    SRC : STRING;
    REP : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := REPLACE_CHARS(STR := STR, SRC := SRC, REP := REP);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
