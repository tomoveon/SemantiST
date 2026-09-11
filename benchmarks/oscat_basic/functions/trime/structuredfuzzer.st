FUNCTION TRIME : STRING
VAR_INPUT
	str : STRING;
END_VAR


(*
version 1.0	28. mar. 2008
programmer 	hugo
tested by	tobias

this function deletes all leading and ending blanks of a string.

*)
(* @END_DECLARATION := '0' *)
TRIME := str;
(* beginning blanks need to be stripped off *)
WHILE LEFT(TRIME,1) = ' ' DO
	TRIME := DELETE(TRIME,1,1);
END_WHILE;

(* ending blanks need to be stripped off *)
WHILE RIGHT(TRIME,1) = ' ' DO
	TRIME := DELETE(TRIME,1,LEN(TRIME));
END_WHILE;


(* revision histroy
hm	28. mar. 2008	rev 1.0
	original release

*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    str : STRING;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := TRIME(str := str);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
