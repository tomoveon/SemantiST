FUNCTION SUM3 : INT;
VAR_INPUT
    a, b, c : INT;
END_VAR
SUM3 := a + b + c;
END_FUNCTION

FUNCTION_BLOCK Poly
VAR
    k : LREAL := 2.0;
END_VAR

METHOD PUBLIC PolyEval : LREAL;
VAR_INPUT
    x : LREAL;
END_VAR
PolyEval := k * x + 1.0;
END_METHOD
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    p : Poly;
END_VAR
printf('%d %f$N', SUM3(1, 2, 3), p.PolyEval(x := 3.0));
main := 0;
END_FUNCTION
