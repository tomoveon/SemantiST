PROGRAM PLC_PRG
VAR_INPUT
    E0 : BOOL;
    E1 : BOOL;
    E2 : BOOL;
    E3 : BOOL;
    C0 : UINT;
    C1 : UINT;
    C2 : UINT;
    C3 : UINT;
    O0 : UINT;
    O1 : UINT;
    O2 : UINT;
    O3 : UINT;
END_VAR
VAR
    semantist_fb : SCHEDULER_2;
    semantist_out_Q0 : BOOL;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
    semantist_out_Q3 : BOOL;
END_VAR
semantist_fb(E0 := E0, E1 := E1, E2 := E2, E3 := E3, C0 := C0, C1 := C1, C2 := C2, C3 := C3, O0 := O0, O1 := O1, O2 := O2, O3 := O3);
semantist_out_Q0 := semantist_fb.Q0;
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
semantist_out_Q3 := semantist_fb.Q3;
END_PROGRAM
