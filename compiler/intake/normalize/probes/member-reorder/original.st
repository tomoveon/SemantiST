(* TwinCAT/PLCopen exports keep methods and actions after the block body. *)
FUNCTION_BLOCK FB_AXIS
VAR
    pos : DINT;
END_VAR
pos := pos + 1;

METHOD MoveBy : DINT
VAR_INPUT
    delta : DINT;
END_VAR
pos := pos + delta;
MoveBy := pos;
END_METHOD

ACTION Reset
pos := 0;
END_ACTION

END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    axis : FB_AXIS;
END_VAR
axis();
axis.MoveBy(10);
printf('%d$N', axis.pos);
axis.Reset();
printf('%d$N', axis.pos);
main := 0;
END_FUNCTION
