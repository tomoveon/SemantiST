(* Platform calibration cases shared by CODESYS and RuSTy.

   Each function maps a case id (see ../cases.csv) to one observed value. The
   same source is compiled by both toolchains, so any difference in the
   recorded values is a difference in platform semantics. Ids 201-208 and
   851-853 may raise a runtime exception on CODESYS and must be run one at a
   time (see ../README_zh.md). *)

FUNCTION CALIB_INT : LINT
VAR_INPUT
    id : INT;
END_VAR
VAR
    w : WORD;
    i : INT;
    i2 : INT;
    z : INT;
    u : UINT;
    d : DINT;
    dz : DINT;
    ud : UDINT;
    udz : UDINT;
    li : LINT;
    bv : BYTE;
    dw : DWORD;
    si : SINT;
    us : USINT;
    r : REAL;
    lr : LREAL;
    sz : UDINT;
    tv : TIME;
    ltv : LTIME;
    dv : DATE;
    dtv : DT;
    todv : TOD;
    t1 : TIME;
    t2 : TIME;
    tn : TIME;
    tx : TIME;
    sa : STRING[200];
    sb : STRING[200];
    k : INT;
END_VAR

CALIB_INT := -999999;
CASE id OF
    (* A: width of intermediate results; wrap only on assignment? *)
    101: w := 65535; CALIB_INT := SEL((w + 1) > 65535, 0, 1);
    102: i := 32767; CALIB_INT := SEL((i + 1) > 32767, 0, 1);
    103: i := 32767; i := i + 1; CALIB_INT := i;
    104: u := 100; d := u * 3600; CALIB_INT := d;
    105: u := 100; CALIB_INT := SEL((u * 3600) > 300000, 0, 1);
    106: bv := 0; CALIB_INT := SEL((bv - 1) = 255, 0, 1);
    107: d := 2147483647; CALIB_INT := SEL((d + 1) > 0, 0, 1);
    108: dw := 16#01000000; CALIB_INT := DWORD_TO_LINT(SHL(dw, 8) / 256);
    109: dw := 16#01000000; dw := SHL(dw, 8); CALIB_INT := DWORD_TO_LINT(dw / 256);
    110: i := 30000; CALIB_INT := SEL((i + i) < 0, 0, 1);
    111: li := 9223372036854775807; CALIB_INT := SEL((li + 1) > 0, 0, 1);
    112: i := -32768; i2 := ABS(i); CALIB_INT := i2;
    113: i := -32768; i2 := -i; CALIB_INT := i2;
    114: si := 127; si := si + 1; CALIB_INT := si;
    115: us := 0; us := us - 1; CALIB_INT := us;
    116: i := DINT_TO_INT(40000); CALIB_INT := i;
    117: CALIB_INT := BYTE_TO_LINT(INT_TO_BYTE(300));
    118: u := INT_TO_UINT(-1); CALIB_INT := u;

    (* B: division and modulo by zero or overflow; may trap, run singly *)
    201: i := 7; z := 0; i2 := i / z; CALIB_INT := i2;
    202: i := 7; z := 0; i2 := i MOD z; CALIB_INT := i2;
    203: d := 7; dz := 0; CALIB_INT := d / dz;
    204: d := 7; dz := 0; CALIB_INT := d MOD dz;
    205: ud := 7; udz := 0; CALIB_INT := ud / udz;
    206: d := -2147483647 - 1; dz := -1; CALIB_INT := d / dz;
    207: li := -9223372036854775807 - 1; CALIB_INT := li / -1;
    208: i := -32768; z := -1; i2 := i / z; CALIB_INT := i2;

    (* C: REAL to integer conversion, range and rounding *)
    301: r := 3.0E9; CALIB_INT := REAL_TO_DINT(r);
    302: r := -3.0E9; CALIB_INT := REAL_TO_DINT(r);
    303: r := 40000.0; CALIB_INT := REAL_TO_INT(r);
    304: r := 2.5; CALIB_INT := REAL_TO_DINT(r);
    305: r := 3.5; CALIB_INT := REAL_TO_DINT(r);
    306: r := -2.5; CALIB_INT := REAL_TO_DINT(r);
    307: lr := 1.0E300; CALIB_INT := LREAL_TO_DINT(lr);
    308: lr := 1.0E19; CALIB_INT := LREAL_TO_LINT(lr);
    309: r := -1.0; CALIB_INT := REAL_TO_UDINT(r);

    (* D: TIME width, unit, wrap and sub-millisecond values *)
    401: sz := SIZEOF(tv); CALIB_INT := sz;
    402: sz := SIZEOF(ltv); CALIB_INT := sz;
    403: sz := SIZEOF(dv); CALIB_INT := sz;
    404: sz := SIZEOF(dtv); CALIB_INT := sz;
    405: sz := SIZEOF(todv); CALIB_INT := sz;
    406: CALIB_INT := DWORD_TO_LINT(TIME_TO_DWORD(T#1ms));
    407: CALIB_INT := DWORD_TO_LINT(TIME_TO_DWORD(T#1s));
    408: t1 := DWORD_TO_TIME(16#FFFFFFFF); t2 := t1 + T#1ms; CALIB_INT := DWORD_TO_LINT(TIME_TO_DWORD(t2));
    409: t1 := DWORD_TO_TIME(16#FFFFFFFF); t2 := t1 + T#1ms; CALIB_INT := SEL(t2 > t1, 0, 1);
    410: tn := DWORD_TO_TIME(16#FFFFFF00); tx := DWORD_TO_TIME(16#00000100); CALIB_INT := SEL((tx - tn) >= T#500ms, 0, 1);
    411: tn := DWORD_TO_TIME(16#FFFFFF00); tx := DWORD_TO_TIME(16#00000100); CALIB_INT := DWORD_TO_LINT(TIME_TO_DWORD(tx - tn));
    412: tv := DWORD_TO_TIME(16#80000000); CALIB_INT := SEL(tv > T#0ms, 0, 1);
    413: tv := DWORD_TO_TIME(16#80000000); CALIB_INT := TIME_TO_DINT(tv);
    414: CALIB_INT := TIME_TO_DINT(T#1ms);
    415: tv := T#1ms; tv := tv / 2; CALIB_INT := DWORD_TO_LINT(TIME_TO_DWORD(tv));
    416: tv := T#1ms; tv := tv / 2; CALIB_INT := SEL(tv > T#0ms, 0, 1);

    (* E: STRING bytes and boundary behaviour of standard functions *)
    501: CALIB_INT := LEN('$D7');
    502: CALIB_INT := LEN(CONCAT('$D7', 'A'));
    503: CALIB_INT := LEN(LEFT('abc', 10));
    504: CALIB_INT := LEN(MID('abc', 5, 2));
    505: CALIB_INT := LEN(MID('abc', 2, 5));
    506: CALIB_INT := LEN(RIGHT('abc', 10));
    507: CALIB_INT := LEN(DELETE('abc', 5, 2));
    508: CALIB_INT := LEN(INSERT('abc', 'X', 10));
    509: CALIB_INT := FIND('abc', '');
    510: CALIB_INT := LEN(LEFT('abc', -1));
    511: sa := ''; FOR k := 1 TO 200 DO sa := CONCAT(sa, 'a'); END_FOR; sb := sa; CALIB_INT := LEN(CONCAT(sa, sb));
    512: CALIB_INT := STRING_TO_INT('abc');
    513: CALIB_INT := STRING_TO_INT('99999');
    514: CALIB_INT := LEN(INT_TO_STRING(-32768));

    (* F: DATE and DT range *)
    601: dtv := DWORD_TO_DT(16#FFFFFFFF); dv := DT_TO_DATE(dtv); CALIB_INT := DWORD_TO_LINT(DATE_TO_DWORD(dv));
    602: dtv := DWORD_TO_DT(1); CALIB_INT := DWORD_TO_LINT(DT_TO_DWORD(dtv));
END_CASE
END_FUNCTION


FUNCTION CALIB_REAL : LREAL
VAR_INPUT
    id : INT;
END_VAR
VAR
    r : REAL;
    rz : REAL;
    lr : LREAL;
    lrz : LREAL;
    d : DINT;
END_VAR

CALIB_REAL := -999999.0;
CASE id OF
    801: r := 16777216.0; r := r + 1.0; CALIB_REAL := r;
    802: lr := 1.0E16; lr := lr + 1.0; CALIB_REAL := lr - 1.0E16;
    803: r := -1.0; CALIB_REAL := SQRT(r);
    804: d := 16777217; CALIB_REAL := DINT_TO_REAL(d);
    805: CALIB_REAL := TIME_TO_REAL(T#1500ms);
    806: r := 1.0E38; r := r * 10.0; CALIB_REAL := r;

    (* may trap, run singly *)
    851: r := 1.0; rz := 0.0; CALIB_REAL := r / rz;
    852: rz := 0.0; CALIB_REAL := rz / rz;
    853: lr := 1.0; lrz := 0.0; CALIB_REAL := lr / lrz;
END_CASE
END_FUNCTION


FUNCTION CALIB_STR : STRING[80]
VAR_INPUT
    id : INT;
END_VAR

CALIB_STR := '?';
CASE id OF
    901: CALIB_STR := LEFT('abc', 10);
    902: CALIB_STR := MID('abc', 5, 2);
    903: CALIB_STR := MID('abc', 2, 5);
    904: CALIB_STR := RIGHT('abc', 10);
    905: CALIB_STR := DELETE('abc', 5, 2);
    906: CALIB_STR := INSERT('abc', 'X', 10);
    907: CALIB_STR := REPLACE('abc', 'X', 10, 2);
    908: CALIB_STR := CONCAT('$D7', 'A');
    909: CALIB_STR := INT_TO_STRING(-32768);
    910: CALIB_STR := LEFT('abc', -1);
END_CASE
END_FUNCTION
