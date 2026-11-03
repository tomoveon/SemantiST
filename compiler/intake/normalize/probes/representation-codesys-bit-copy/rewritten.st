(* CODESYS converts the value: DWORD_TO_REAL(1000) = 1000.0. The pinned RuSTy accepts
   the call but copies the bit pattern (IEC binary transfer). *)
FUNCTION main : DINT
VAR
    r : REAL;
END_VAR
r := __CDS_DWORD_TO_REAL(DWORD#1065353216);
printf('%d$N', REAL_TO_DINT(r));
main := 0;
END_FUNCTION
