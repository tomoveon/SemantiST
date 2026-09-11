FUNCTION SECOND : REAL
VAR_INPUT
	itod : TOD;
END_VAR


(*
version 1.1	2 oct 2006
programmer 	hugo
tested by	oscat

returns the seconds and milliseconds as real of TOD   
 
*)
(* @END_DECLARATION := '0' *)
SECOND := DWORD_TO_REAL(TOD_TO_DWORD(itod) - TOD_TO_DWORD(itod)/60000 * 60000) / 1000.0;



(* change history

hm	2. oct. 2006 rev 1.1 
	changed name of input to itod

*)
END_FUNCTION


FUNCTION MONTH_OF_DATE : INT
VAR_INPUT
	IDATE : DATE;
END_VAR


(*
version 1.3	27. mar. 2009
programmer 	hugo
tested by		tobias

returns the current month of the year.

*)
(* @END_DECLARATION := '0' *)
MONTH_OF_DATE := DAY_OF_YEAR(idate);
IF MONTH_OF_DATE < 32 THEN
	MONTH_OF_DATE := 1;
ELSIF LEAP_OF_DATE(IDATE) THEN
	MONTH_OF_DATE := (MONTH_OF_DATE * 53 + 1668) / 1623;
ELSE
	MONTH_OF_DATE := (MONTH_OF_DATE * 53 + 1700) / 1620;
END_IF;


(* code for rev 1.2
MONTH_OF_DATE := DAY_OF_YEAR(IDATE);
IF LEAP_OF_DATE(IDATE) THEN
	CASE MONTH_OF_DATE OF
		1..31 	:	MONTH_OF_DATE := 1;
		32..60	:	MONTH_OF_DATE := 2;
		61..91	:	MONTH_OF_DATE := 3;
		92..121 :	MONTH_OF_DATE := 4;
		122..152:	MONTH_OF_DATE := 5;
		153..182:	MONTH_OF_DATE := 6;
		183..213:	MONTH_OF_DATE := 7;
		214..244:	MONTH_OF_DATE := 8;
		245..274:	MONTH_OF_DATE := 9;
		275..305:	MONTH_OF_DATE := 10;
		306..335:	MONTH_OF_DATE := 11;
		336..366:	MONTH_OF_DATE := 12;
	END_CASE;
ELSE
	CASE MONTH_OF_DATE OF
		1..31 	:	MONTH_OF_DATE := 1;
		32..59	:	MONTH_OF_DATE := 2;
		60..90	:	MONTH_OF_DATE := 3;
		91..120 :	MONTH_OF_DATE := 4;
		121..151:	MONTH_OF_DATE := 5;
		152..181:	MONTH_OF_DATE := 6;
		182..212:	MONTH_OF_DATE := 7;
		213..243:	MONTH_OF_DATE := 8;
		244..273:	MONTH_OF_DATE := 9;
		274..304:	MONTH_OF_DATE := 10;
		305..334:	MONTH_OF_DATE := 11;
		335..365:	MONTH_OF_DATE := 12;
	END_CASE;
END_IF;
*)


(* revision history
hm	1. aug 2006	rev 1.0
	original version	

hm	1. okt 2007	rev 1.1
	replaced old code (string conversion) with mathematics
	the execution time is now multiple times faster.

hm	7. oct. 2008	rev 1.2
	changed name of function from month to MONTH_OF_DATE

hm	27. mar. 2009	rev 1.3
	new improved code

*)
END_FUNCTION


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


FUNCTION WEEKDAY_TO_STRING : STRING
VAR_INPUT
	WDAY : INT;
	LANG : INT;
	LX : INT;
END_VAR
VAR
	ly : INT;
END_VAR

(*
version 1.1	19. oct. 2008
programmer 	hugo
tested by	oscat

WEEKDAY_TO_STRING converts an Integer 1..7 into a String with the Names of the corresponding weekday.
the decoding is according to the language setup in global vars

*)
(* @END_DECLARATION := '0' *)
IF LANG = 0 THEN ly := LANGUAGE.DEFAULT; ELSE ly := MIN(LANG, LANGUAGE.LMAX); END_IF;
IF wday < 1 OR wday > 7 THEN
	RETURN;
ELSIF LX = 0 THEN
	WEEKDAY_TO_STRING := language.WEEKDAYS[ly, WDAY];
ELSIF Lx = 2 THEN
	WEEKDAY_TO_STRING := language.WEEKDAYS2[ly, WDAY];
END_IF;


(* revisison history

hm	21. sep. 2008	rev 1.0
	original version

hm	19. oct. 2008	rev 1.1
	changed language setup constants
*)
END_FUNCTION


FUNCTION MINUTE : INT
VAR_INPUT
	itod : TOD;
END_VAR


(*
version 1.1	2 oct 2006
programmer 	hugo
tested by	tobias

extracts the minutes out of TOD truncating the seconds 

*)  
(* @END_DECLARATION := '0' *)
MINUTE := DWORD_TO_INT(TOD_TO_DWORD(itod) / 60000 - TOD_TO_DWORD(itod) / 3600000 * 60);


(* change history

2.10.2006 changes name of input to itod

*)
END_FUNCTION


FUNCTION DAY_OF_MONTH : INT
VAR_INPUT
	IDATE : DATE;
END_VAR
VAR
	leap: INT;
END_VAR

(*
version 2.1	10. mar. 2009
programmer 	hugo
tested by		tobias

returns the day OF month for any DATE

*)
(* @END_DECLARATION := '0' *)
(* calculate the day in the year *)
DAY_OF_MONTH := DAY_OF_YEAR(idate);
(* leap will be set to one for a leap year *)
leap := BOOL_TO_INT(LEAP_OF_DATE(idate));
(* if leap year deduct one from the days of the year *)
DAY_OF_MONTH := DAY_OF_MONTH - leap;
(* search if we are in month december to march ? *)
IF DAY_OF_MONTH > setup.MTH_OFS[9] THEN
	IF DAY_OF_MONTH > setup.MTH_OFS[11] THEN
		IF DAY_OF_MONTH > setup.mth_ofs[12] THEN
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[12];
		ELSE
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[11];
		END_IF;
	ELSE
		IF DAY_OF_MONTH > setup.mth_ofs[10] THEN
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[10];
		ELSE
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[9];
		END_IF;
	END_IF;
ELSIF DAY_OF_MONTH > setup.MTH_OFS[5] THEN
	IF DAY_OF_MONTH > setup.MTH_OFS[7] THEN
		IF DAY_OF_MONTH > setup.mth_ofs[8] THEN
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[8];
		ELSE
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[7];
		END_IF;
	ELSE
		IF DAY_OF_MONTH > setup.mth_ofs[6] THEN
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[6];
		ELSE
			DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[5];
		END_IF;
	END_IF;
ELSIF DAY_OF_MONTH > setup.MTH_OFS[3] THEN
	IF DAY_OF_MONTH > setup.MTH_OFS[4] THEN
		DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[4];
	ELSE
		DAY_OF_MONTH := DAY_OF_MONTH - setup.MTH_OFS[3];
	END_IF;
ELSE
	(* since now we must be in february or january we need to add leap again *)
	DAY_OF_MONTH := DAY_OF_MONTH + leap;
	IF DAY_OF_MONTH > setup.MTH_OFS[2] THEN DAY_OF_MONTH := DAY_OF_MONTH - setup.mth_ofs[2]; END_IF;
	(* since nothing was true before, day_of_month must already be good *)
END_IF;


(*
Revision history

hm 22.1.2007		rev 1.1
	deleted unused variable day_in_year and day_in_year_begin

hm	1. okt 2007	rev 1.2
	changed code to use day_of_year and leap_of_date
	added compatibility to STEP7

hm	8. oct 2007	rev 1.3
	deleted unused variable yr

hm	8. jan 2008	rev 1.4
	improved performance

hm	25. oct. 2008	rev 2.0
	new code using setup constants

hm	10. mar. 2009	rev 2.1
	removed nested comments

*)


END_FUNCTION


FUNCTION HOUR : INT
VAR_INPUT
	itod : TOD;
END_VAR


(*
version 1.1	2 okt 2006
programmer 	hugo
tested by	tobias

extracts the hour of a Time_of_day 
*)
(* @END_DECLARATION := '0' *)
HOUR := DWORD_TO_INT(TOD_TO_DWORD(itod) / 3600000);


(* change history
hm 4. aug 2006	rev 1.0
	original version

hm 2.10.2006 	rev 1.1
	changed name of input to itod

*)
END_FUNCTION


FUNCTION YEAR_OF_DATE : INT
VAR_INPUT
	IDATE : DATE;
END_VAR


(*
version 1.4	7. oct. 2008
programmer 	hugo
tested by		oscat

returs the year of a date  
the function works for dates from 1970 - 2099 

*)

(* @END_DECLARATION := '0' *)
YEAR_OF_DATE := DWORD_TO_INT((DATE_TO_DWORD(idate) + 43200) / 31557600 + 1970);


(* revision history
hm	4. aug 2006		rev 1.0
	original version

hm	1. okt 2007		rev 1.1
	corrected error in algorithm
	adjustment for S7 compatibility

hm	23.12.2007		rev 1.2
	changed code for better performance

hm	7. apr. 2008	rev 1.3
	deleted unused step7 code

hm	7. oct. 2008	rev 1.4
	renamed function (year) to year_of_date

*)
END_FUNCTION


FUNCTION MONTH_TO_STRING : STRING
VAR_INPUT
	MTH : INT;
	LANG : INT;
	LX : INT;
END_VAR
VAR
	ly : INT;
END_VAR

(*
version 1.1	19. oct. 2008
programmer 	hugo
tested by	oscat

MONTH_TO_STRING converts an Integer 1..12 into a String with the Names of the corresponding Month.
the decoding is according to the language setup in global vars

*)
(* @END_DECLARATION := '0' *)
IF LANG = 0 THEN Ly := LANGUAGE.DEFAULT; ELSE ly := MIN(LANG, language.LMAX); END_IF;
IF MTH < 1 OR MTH > 12 THEN
	RETURN;
ELSIF LX = 0 THEN
	MONTH_TO_STRING := language.MONTHS[ly, MTH];
ELSIF Lx = 3 THEN
	MONTH_TO_STRING := language.MONTHS3[ly, MTH];
END_IF;


(* revisison history

hm	21. sep. 2008	rev 1.0
	original version

hm	19. oct. 2008	rev 1.1
	changed language setup constants
*)
END_FUNCTION


FUNCTION CODE : BYTE
VAR_INPUT
	STR : STRING;
	POS : INT;
END_VAR
VAR
	PT : REF_TO BYTE;
END_VAR

(*
version 1.1	29. mar. 2008
programmer 	hugo
tested by		tobias

code extracts the code of a character at position POS of a string STR.

*)
(* @END_DECLARATION := '0' *)
IF pos < 1 OR pos > LEN(str) THEN
	CODE := 0;
	RETURN;
ELSE
	PT := REF(STR) + INT_TO_DWORD(pos - 1);
	CODE := pt^;
END_IF;

(* revision history
hm	9. mar. 2008	rev 1.0
	original version

hm	29. mar. 2008	rev 1.1
	changed STRING to STRING
*)
END_FUNCTION


FUNCTION DT_TO_STRF : STRING
VAR_INPUT
	DTI : DT;
	MS : INT;
	FMT : STRING;
	LANG : INT;
END_VAR
VAR
	FILL : STRING := '0';
	BLANK : STRING := ' ';
END_VAR
VAR
	ly : INT;
	dx: DATE;
	fs: STRING;
	td: TOD;
	tmp : INT;
	pos : INT;
	f : INT;
END_VAR

(*
version 1.1	19. oct. 2008
programmer 	hugo
tested by	oscat

DT_TO_STRINGF converts a DATETIME input to a formatted string

*)
(* @END_DECLARATION := '0' *)
IF LANG < 1 THEN ly := language.DEFAULT; ELSE ly := MIN(language.LMAX, LANG); END_IF;

(* decode date and time information *)
dx := DT_TO_DATE(DTI);
td := DT_TO_TOD(DTI);

(* parse the format string *)
DT_TO_STRF := FMT;
pos := FIND(DT_TO_STRF, '#');
WHILE pos > 0 DO
	(* retrieve format identifier *)
	f := CODE(DT_TO_STRF, pos + 1);
	(* generate the return string according to the format character *)
	fs := '';
	CASE f OF
		65 : (* letter A retunrs the year in 4 digits *)
			fs := INT_TO_STRING(YEAR_OF_DATE(dx));
		66 : (* letter B returns the year in exactly 2 digits *)
			fs := RIGHT(INT_TO_STRING(YEAR_OF_DATE(dx)),2);
		67 : (* letter C returns the month with 1 or 2 digits *)
			fs := INT_TO_STRING(MONTH_OF_DATE(dx));
		68 : (* letter D returns the month with exactly 2 digits *)
			fs := INT_TO_STRING(MONTH_OF_DATE(dx));
			IF LEN(fs) < 2 THEN fs := CONCAT('0', fs); END_IF;
		69 : (* letter E returns the month with 3 characters *)
			fs := MONTH_TO_STRING(MONTH_OF_DATE(dx), ly, 3);
		70 : (* letter F returns the month with all characters *)
			fs := MONTH_TO_STRING(MONTH_OF_DATE(dx), ly, 0);
		71 : (* letter G returns the day with up to 2 digits *)
			fs := INT_TO_STRING(DAY_OF_MONTH(dx));
		72 : (* letter H returns the day of the month with exactly 2 digits *)
			fs := INT_TO_STRING(DAY_OF_MONTH(dx));
			IF LEN(fs) < 2 THEN fs := CONCAT(FILL, fs); END_IF;
		73 : (* letter I returns the weekday as the number 1..7 1 = monday *)
			fs := INT_TO_STRING(DAY_OF_WEEK(dx));
		74 : (* letter J returns the weekday in 2 character writing *)
			fs := WEEKDAY_TO_STRING(DAY_OF_WEEK(dx), ly, 2);
		75 : (* letter K returns the weekday with all characters *)
			fs := WEEKDAY_TO_STRING(DAY_OF_WEEK(dx), ly, 0);
		76 : (* letter L returns AM or PM for the given DateTime *)
			IF td >= TOD#12:00:00 THEN fs := 'PM'; ELSE fs := 'AM'; END_IF;
		77 : (* letter M returns the hour in 1 or 2 digit form 0..24h *)
			fs := INT_TO_STRING(HOUR(td));
		78 : (* letter N returns the hour in exactly 2 digit form 0..24h *)
			fs := INT_TO_STRING(HOUR(td));
			IF LEN(fs) < 2 THEN fs := CONCAT(FILL, fs); END_IF;
		79 : (* letter O returns the hour in 1 or 2 digit form 0..12h *)
			tmp := HOUR(td) MOD 12;
			IF tmp = 0 THEN tmp := 12; END_IF;
			fs := INT_TO_STRING(tmp);
		80 : (* letter P returns the hour in exactly 2 digit form 0..12h *)
			tmp := HOUR(td) MOD 12;
			IF tmp = 0 THEN tmp := 12; END_IF;
			fs := INT_TO_STRING(tmp);
			IF LEN(fs) < 2 THEN fs := CONCAT(FILL, fs); END_IF;
		81 : (* letter Q returns the minute of the hour in 1 or two digit form *)
			fs := INT_TO_STRING(MINUTE(td));
		82 : (* letter R returns the minute of the hour in exactly two digit form *)
			fs := INT_TO_STRING(MINUTE(td));
			IF LEN(fs) < 2 THEN fs := CONCAT(FILL, fs); END_IF;
		83 : (* letter S returns the second of the minute in 1 or two digit form *)
			fs := INT_TO_STRING(REAL_TO_INT(SECOND(td)));
		84 : (* letter T returns the second of the minute in exactly two digit form *)
			fs := INT_TO_STRING(REAL_TO_INT(SECOND(td)));
			IF LEN(fs) < 2 THEN fs := CONCAT(FILL, fs); END_IF;
		85 : (* letter U returns the milliseconds in 1 to 3 digits *)
			fs := INT_TO_STRING(MS);
		86 : (* letter V returns the milliseconds in exactly 3 digit form *)
			fs := INT_TO_STRING(MS);
			fs := CONCAT('00',fs);
			fs := RIGHT(fs, 3);
		87 : (* letter W returns the day of the month with exactly 2 digits first digit is filled with blank if necessary *)
			fs := INT_TO_STRING(DAY_OF_MONTH(dx));
			IF LEN(fs) < 2 THEN fs := CONCAT(BLANK, fs); END_IF;
		88 : (* letter X returns the month with exactly 2 digits first digit is filled with blank if necessary *)
			fs := INT_TO_STRING(MONTH_OF_DATE(dx));
			IF LEN(fs) < 2 THEN fs := CONCAT(BLANK, fs); END_IF;
		END_CASE;
	DT_TO_STRF := REPLACE(DT_TO_STRF, fs, 2, pos);
	pos := FIND(DT_TO_STRF, '#');
END_WHILE;


(* revision history
hm	7. oct. 2008	rev 1.0
	original version

hm	19. oct. 2008	rev 1.1
	changed language setup constants
*)

END_FUNCTION


PROGRAM PLC_PRG
VAR_INPUT
    DTI : DT;
    MS : INT;
    FMT : STRING;
    LANG : INT;
END_VAR
VAR
    semantist_result : STRING;
END_VAR
semantist_result := DT_TO_STRF(DTI := DTI, MS := MS, FMT := FMT, LANG := LANG);
END_PROGRAM


CONFIGURATION STD_CONF
  RESOURCE STD_RESSOURCE ON PLC
    TASK TaskMain(INTERVAL := T#50ms,PRIORITY := 0);
    PROGRAM Inst0 WITH TaskMain : PLC_PRG;
  END_RESOURCE
END_CONFIGURATION
