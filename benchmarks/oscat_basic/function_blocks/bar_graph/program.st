PROGRAM PLC_PRG
VAR_INPUT
    X : REAL;
    rst : BOOL;
    trigger_Low : REAL;
    trigger_High : REAL;
    Alarm_low : BOOL;
    Alarm_high : BOOL;
    log_scale : BOOL;
END_VAR
VAR
    semantist_fb : BAR_GRAPH;
    semantist_out_LOW : BOOL;
    semantist_out_Q1 : BOOL;
    semantist_out_Q2 : BOOL;
    semantist_out_Q3 : BOOL;
    semantist_out_Q4 : BOOL;
    semantist_out_Q5 : BOOL;
    semantist_out_Q6 : BOOL;
    semantist_out_HIGH : BOOL;
    semantist_out_Alarm : BOOL;
    semantist_out_Status : BYTE;
END_VAR
semantist_fb(X := X, rst := rst, trigger_Low := trigger_Low, trigger_High := trigger_High, Alarm_low := Alarm_low, Alarm_high := Alarm_high, log_scale := log_scale);
semantist_out_LOW := semantist_fb.LOW;
semantist_out_Q1 := semantist_fb.Q1;
semantist_out_Q2 := semantist_fb.Q2;
semantist_out_Q3 := semantist_fb.Q3;
semantist_out_Q4 := semantist_fb.Q4;
semantist_out_Q5 := semantist_fb.Q5;
semantist_out_Q6 := semantist_fb.Q6;
semantist_out_HIGH := semantist_fb.HIGH;
semantist_out_Alarm := semantist_fb.Alarm;
semantist_out_Status := semantist_fb.Status;
END_PROGRAM
