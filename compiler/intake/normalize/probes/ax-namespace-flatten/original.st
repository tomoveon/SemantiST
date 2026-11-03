(* Source: lib/Counter.st *)
NAMESPACE Vendor.Tools
    CLASS Counter
        VAR
            n : INT;
        END_VAR
        METHOD Next : INT
            n := n + 1;
            Next := n;
        END_METHOD
    END_CLASS
    TYPE
        Mode : (Off := 0, On := 1);
    END_TYPE
    FUNCTION Twice : INT
        VAR_INPUT x : INT; END_VAR
        Twice := 2 * x;
    END_FUNCTION
END_NAMESPACE

(* Source: app/Main.st *)
USING Vendor.Tools;
FUNCTION main : DINT
VAR
    c : Counter;
    q : Vendor.Tools.Counter;
    m : Mode := Mode#Off;
END_VAR
m := Vendor.Tools.Mode#On;
c.Next();
printf('%d %d %d$N', Twice(c.Next()), q.Next(), m);
main := 0;
END_FUNCTION
