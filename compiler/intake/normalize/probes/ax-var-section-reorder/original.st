CLASS Counter
    VAR PUBLIC
        step : INT := 2;
    END_VAR
    METHOD Next : INT
        n := n + step;
        Next := n;
    END_METHOD
    VAR PRIVATE
        n : INT := 1;
    END_VAR
    METHOD Get : INT
        Get := n;
    END_METHOD
END_CLASS

FUNCTION main : DINT
VAR
    c : Counter;
END_VAR
c.Next();
printf('%d %d$N', c.Next(), c.Get());
main := 0;
END_FUNCTION
