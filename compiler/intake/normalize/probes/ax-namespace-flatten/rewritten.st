(* Source: lib/Counter.st *)
(* NAMESPACE Vendor.Tools *)
    CLASS Vendor_Tools__Counter
        VAR
            n : INT;
        END_VAR
        METHOD Next : INT
            n := n + 1;
            Next := n;
        END_METHOD
    END_CLASS
    TYPE
        Vendor_Tools__Mode : (Off := 0, On := 1);
    END_TYPE
    FUNCTION Vendor_Tools__Twice : INT
        VAR_INPUT x : INT; END_VAR
        Vendor_Tools__Twice := 2 * x;
    END_FUNCTION
(* END_NAMESPACE *)

(* Source: app/Main.st *)
(* USING Vendor.Tools; *)
FUNCTION main : DINT
VAR
    c : Vendor_Tools__Counter;
    q : Vendor_Tools__Counter;
    m : Vendor_Tools__Mode := Vendor_Tools__Mode#Off;
END_VAR
m := Vendor_Tools__Mode#On;
c.Next();
printf('%d %d %d$N', Vendor_Tools__Twice(c.Next()), q.Next(), m);
main := 0;
END_FUNCTION
