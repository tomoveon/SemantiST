(* TwinCAT code names standard blocks through their library namespace. *)
FUNCTION main : DINT
VAR
    edge : Tc2_Standard.R_TRIG;
    n : DINT;
END_VAR
edge(CLK := FALSE); IF edge.Q THEN n := n + 1; END_IF;
edge(CLK := TRUE); IF edge.Q THEN n := n + 1; END_IF;
edge(CLK := TRUE); IF edge.Q THEN n := n + 1; END_IF;
printf('%d$N', n);
main := 0;
END_FUNCTION
