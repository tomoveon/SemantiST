FUNCTION HEX_TO_DWORD : DWORD
VAR_INPUT
	Hex : STRING;
END_VAR
VAR
	pt : REF_TO BYTE;
	i: INT;
	X: BYTE;
	stop: INT;
END_VAR

(*
version 1.4	18. jun. 2008
programmer 	hugo
tested by	tobias

HEX_TO_DWORD converts a Hexadecimal string into a DWORD.

*)
(* @END_DECLARATION := '0' *)
pt := REF(hex);
stop := LEN(hex);
FOR I := 1 TO stop DO
	(* read the first character and subtract 48 to get value in decimal 0 = 48 *)
	x := pt^;
	(* calculate the value of the digit *)
	IF X > 47 AND x < 58 THEN
		HEX_TO_DWORD := SHL(HEX_TO_DWORD,4) + X - 48;
	ELSIF X > 64 AND X < 71 THEN
		HEX_TO_DWORD := SHL(HEX_TO_DWORD,4) + X - 55;
	ELSIF X > 96 AND X < 103 THEN
		HEX_TO_DWORD := SHL(HEX_TO_DWORD,4) + X - 87;
	END_IF;
	pt := pt + 1;
END_FOR;



(* revision histroy
hm	2.10.2007		rev 1.0
	original release

hm	19.11.2007		rev 1.1
	changed type of function from int to dword

hm 	4. mar 2008		rev 1.2
	added support for a..f and return 0 for invalid string

hm	29. mar. 2008	rev 1.3
	changed STRING to STRING

hm	18. jun. 2008	rev 1.4
	changed input hex to STRING
	function now ignores wrong characters
*)	

END_FUNCTION


FUNCTION OCT_TO_DWORD : DWORD
VAR_INPUT
	OCT : STRING;
END_VAR
VAR
	pt : REF_TO BYTE;
	i: INT;
	X: BYTE;
	stop: INT;
END_VAR

(*
version 1.0	18. jun 2008
programmer 	hugo
tested by	oscat

OCT_TO_DWORD converts a octagonal string into a dword.

*)
(* @END_DECLARATION := '0' *)
pt := REF(oct);
stop := LEN(oct);
FOR I := 1 TO stop DO
	(* read the first character and subtract 48 to get value in decimal 0 = 48 *)
	x := pt^;
	(* calculate the value of the digit *)
	IF X > 47 AND x < 56 THEN
		OCT_TO_DWORD := SHL(OCT_TO_DWORD,3) + X - 48;
	END_IF;
	pt := pt + 1;
END_FOR;




(* revision histroy
hm	18. jun. 2008	rev 1.0
	original release


*)	

END_FUNCTION


FUNCTION DEC_TO_DWORD : DWORD
VAR_INPUT
	DEC : STRING;
END_VAR
VAR
	pt : REF_TO BYTE;
	i: INT;
	X: BYTE;
	stop: INT;
END_VAR

(*
version 1.1	30. sep. 2008
programmer 	hugo
tested by		oscat

DEC_TO_DWORD converts a decimal string into a DWORD.

*)
(* @END_DECLARATION := '0' *)
pt := REF(dec);
stop := LEN(dec);
FOR I := 1 TO stop DO
	(* read the first character and subtract 48 to get value in decimal 0 = 48 *)
	x := pt^;
	(* calculate the value of the digit *)
	IF X > 47 AND x < 58 THEN
		DEC_TO_DWORD := DEC_TO_DWORD * 10 + X - 48;
	END_IF;
	pt := pt + 1;
END_FOR;



(* revision histroy
hm	20. jun. 2008	rev 1.0
	original release

hm	30. sep. 2008	rev 1.1
	corrected an error where decoding of characters 8 and 9 would fail
*)	

END_FUNCTION


FUNCTION CLEAN : STRING
VAR_INPUT
	IN : STRING;
	CX : STRING;
END_VAR
VAR
	pos: INT := 1;
	stop: INT;
END_VAR


(*
version 1.0	18. jun. 2008
programmer 	hugo
tested by	oscat

Clean deletes all characters from a string except the ones specified in CX.

*)

(* @END_DECLARATION := '0' *)
(* copy input string *)
CLEAN := IN;
stop := LEN(in);
WHILE pos <= stop DO
	IF FIND(cx, MID(CLEAN, 1, pos)) > 0 THEN
		(* charcter found skip to next one *)
		pos := pos + 1;
	ELSE
		(* wrong chracter needs to be deleted *)
		CLEAN := DELETE(CLEAN, 1, pos);
		stop := stop - 1;	(* the string is one character shorter now *)
	END_IF;
END_WHILE;



(* revision history

hm		18. jun. 2008	rev 1.0		
	original version 


*)
END_FUNCTION


FUNCTION BIN_TO_DWORD : DWORD
VAR_INPUT
	BIN : STRING;
END_VAR
VAR
	pt : REF_TO BYTE;
	i: INT;
	X: BYTE;
	stop: INT;
END_VAR

(*
version 1.2	26. jul. 2009
programmer 	hugo
tested by		oscat

BINARY_TO_DWORD converts a binary string into a dword.

*)
(* @END_DECLARATION := '0' *)
pt := REF(bin);
stop := LEN(bin);
FOR I := 1 TO stop DO
	x := pt^;
	(* calculate the value of the digit *)
	IF X = 48 THEN
		BIN_TO_DWORD := SHL(BIN_TO_DWORD,1);
	ELSIF X = 49 THEN
		BIN_TO_DWORD := SHL(BIN_TO_DWORD,1) OR 1;
	END_IF;
	pt := pt + 1;
END_FOR;



(* revision histroy
hm	18. jun. 2008	rev 1.0
	original release

hm	20. sep. 2008	rev 1.1
	changed length of input dtring from 20 to 40

hm	26. jul 2009	rev 1.2
	optimized code
*)	

END_FUNCTION


FUNCTION FSTRING_TO_DWORD : DWORD
VAR_INPUT
	IN : STRING;
END_VAR


(*
version 1.1	20. sep. 2008
programmer 	hugo
tested by		oscat

FSTRING_TO_BYTE converts a formatted string into a byte.
the string can be of the form 2#01010, 8#7234, 16#2AD3 and 1234

*)
(* @END_DECLARATION := '0' *)
IF LEFT(IN, 2) = '2#' THEN
	(* we need to convert binary *)
	FSTRING_TO_DWORD := BIN_TO_DWORD(RIGHT(in, LEN(in) - 2));
ELSIF LEFT(in, 2) = '8#' THEN
	(* weneed to convert octals *)
	FSTRING_TO_DWORD := OCT_TO_DWORD(RIGHT(in, LEN(in) - 2));
ELSIF LEFT(in, 3) = '16#' THEN
	(* we need to convert hexadecimal *)
	FSTRING_TO_DWORD := HEX_TO_DWORD(RIGHT(in, LEN(in) - 3));
ELSE
	(* we assume decimal representation *)
	FSTRING_TO_DWORD := DEC_TO_DWORD(CLEAN(in,'0123456789'));
END_IF;


(* revision histroy
hm	18. jun. 2008	rev 1.0
	original release

hm	20. sep. 2008	rev 1.1
	changed length of input dtring from 20 to 40


*)	

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    IN : STRING;
END_VAR
VAR
    semantist_result : DWORD;
END_VAR
semantist_result := FSTRING_TO_DWORD(IN := IN);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
