FUNCTION_BLOCK FB_LAST
VAR
meth_last_iLast__INST : INT := 0;
END_VAR
VAR
    unrelated : INT;
END_VAR
METHOD meth_last : INT
VAR_INPUT
    iVar : INT;
END_VAR
(* VAR_INST
    iLast : INT := 0;
END_VAR *)
meth_last := meth_last_iLast__INST;
meth_last_iLast__INST := iVar;
END_METHOD
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    one : FB_LAST;
    two : FB_LAST;
    r1, r2, r3, r4 : INT;
END_VAR
r1 := one.meth_last(iVar := 5);
r2 := two.meth_last(iVar := 9);
r3 := one.meth_last(iVar := 7);
r4 := two.meth_last(iVar := 1);
printf('%d %d %d %d$N', r1, r2, r3, r4);
main := 0;
END_FUNCTION
