FUNCTION LEAP_OF_DATE : BOOL
VAR_INPUT
	idate : DATE;
END_VAR


(*
version 1.3		28. jan. 2011
programmer 	hugo
tested by		tobias

leap_of_date is true if current year is a leap year  

*)

(* @END_DECLARATION := '0' *)
LEAP_OF_DATE := SHL(((DATE_TO_DWORD(idate) + 43200) / 31557600), 30) = 16#80000000;


(* change history

2.10.2006		rev 1.1
the function now calls leap_year to accomodate further accuracy.
the function now works for any year from 1970 to 2100

8. jan 2008		rev 1.2
	improved code for better performance

28. jan. 2011	rev 1.3
	improved performance
*)
END_FUNCTION


FUNCTION DAY_OF_YEAR : INT
VAR_INPUT
	IDATE : DATE;
END_VAR


(*
version 1.4		28. jan. 2011
programmer 	hugo
tested by		oscat

calculates the day of the year

*)
(* @END_DECLARATION := '0' *)
DAY_OF_YEAR := UDINT_TO_INT((DATE_TO_UDINT(idate) / UDINT#86400) MOD UDINT#1461);
IF DAY_OF_YEAR > 729 THEN
	IF DAY_OF_YEAR > 1095 THEN DAY_OF_YEAR := DAY_OF_YEAR - 1095; ELSE DAY_OF_YEAR := DAY_OF_YEAR - 729; END_IF;
ELSIF DAY_OF_YEAR > 364 THEN
	DAY_OF_YEAR := DAY_OF_YEAR - 364;
ELSE
	DAY_OF_YEAR := DAY_OF_YEAR + 1;
END_IF;


(*
DAY_OF_YEAR := DWORD_TO_INT((DATE_TO_DWORD(idate) - DATE_TO_DWORD(YEAR_BEGIN(YEAR_OF_DATE(idate)))) / 86400) + 1;
*)


(* revivision history
hm	4. aug. 2007		rev 1.0
	original version

hm	1. oct. 2007		rev 1.1
	added compatibility to STEP7

hm	4. jan. 2008		rev 1.2
	changed code for better performance

hm	7. oct. 2008		rev 1.3
	changed name of function year to year_of_date

hm	28. jan. 2011	rev 1.4
	improved code
*)
END_FUNCTION


FUNCTION DAYS_IN_MONTH : INT
VAR_INPUT
	IDATE : DATE;
END_VAR


(*
version 1.0	27. mar. 2009
programmer 	hugo
tested by		oscat

returs the total days of the current month. e.g. 31 for january.
the function works for dates from 1970 - 2099

*)

(* @END_DECLARATION := '0' *)
DAYS_IN_MONTH := DAY_OF_YEAR(IDATE);
IF LEAP_OF_DATE(IDATE) THEN
	CASE DAYS_IN_MONTH OF
		32..60	:	DAYS_IN_MONTH := 29;
		92..121 :	DAYS_IN_MONTH := 30;
		153..182:	DAYS_IN_MONTH := 30;
		245..274:	DAYS_IN_MONTH := 30;
		306..335:	DAYS_IN_MONTH := 30;
	ELSE
		DAYS_IN_MONTH := 31;
	END_CASE;
ELSE
	CASE DAYS_IN_MONTH OF
		32..59	:	DAYS_IN_MONTH := 28;
		91..120 :	DAYS_IN_MONTH := 30;
		152..181:	DAYS_IN_MONTH := 30;
		244..273:	DAYS_IN_MONTH := 30;
		305..334:	DAYS_IN_MONTH := 30;
	ELSE
		DAYS_IN_MONTH := 31;
	END_CASE;
END_IF;



(* revision history
hm	27. mar. 2009		rev 1.0
	original version

*)
END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    IDATE : DATE;
END_VAR
VAR
    semantist_result : INT;
END_VAR
semantist_result := DAYS_IN_MONTH(IDATE := IDATE);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
