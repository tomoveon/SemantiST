(* S7: DINT_TO_BOOL copies the least significant bit (DWORD_TO_BOOL(DINT_TO_DWORD(x))),
   so DINT_TO_BOOL(2) is FALSE. The pinned RuSTy accepts the call but tests for non-zero. *)
FUNCTION main : DINT
IF __S7_DINT_TO_BOOL(2) THEN
    printf('1$N');
ELSE
    printf('0$N');
END_IF;
main := 0;
END_FUNCTION
