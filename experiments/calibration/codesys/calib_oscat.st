(* OSCAT Basic cases behind earlier findings; needs the OSCAT library.
   Ids 951 and 952 divide by zero and must be run one at a time. *)

FUNCTION CALIB_OSCAT_STR : STRING[80]
VAR_INPUT
    id : INT;
END_VAR

CALIB_OSCAT_STR := '?';
CASE id OF
    951: CALIB_OSCAT_STR := INT_TO_STRING(INC(X := 0, D := 1, M := -1));
    952: CALIB_OSCAT_STR := INT_TO_STRING(INC2(X := 0, D := 1, L := 0, U := -1));
    953: CALIB_OSCAT_STR := REAL_TO_STRF(IN := -3599.0, N := 6, D := '.');
    954: CALIB_OSCAT_STR := CHARNAME(C := 255);
    955: CALIB_OSCAT_STR := CHARNAME(C := 65);
    956: CALIB_OSCAT_STR := CLEAN(IN := 'a$D7b', CX := 'ab');
END_CASE
END_FUNCTION
