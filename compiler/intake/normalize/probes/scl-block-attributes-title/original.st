FUNCTION_BLOCK TRIPLE
TITLE = 'TRIPLE'
//uses : nothing
//VERSION : '2.0';

VAR_INPUT
    X : INT;
END_VAR
VAR_OUTPUT
    Y : INT;
END_VAR
Y := X * 3;
END_FUNCTION_BLOCK

FUNCTION main : DINT
VAR
    t : TRIPLE;
END_VAR
t(X := 7);
printf('%d$N', t.Y);
main := 0;
END_FUNCTION
