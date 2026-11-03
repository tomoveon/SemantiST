(* CODESYS: TIME_TO_LWORD yields milliseconds and ULINT_TO_TIME reads milliseconds.
   The pinned RuSTy accepts both calls but uses nanoseconds. *)
FUNCTION main : DINT
VAR
    t : TIME := T#1500ms;
END_VAR
printf('%d$N', LWORD_TO_DINT(__CDS_TIME_TO_LWORD(t)));
printf('%d$N', LWORD_TO_DINT(__CDS_TIME_TO_LWORD(__CDS_ULINT_TO_TIME(250))));
main := 0;
END_FUNCTION
