FUNCTION D02_FRAME_CHECK : BYTE
VAR_INPUT
    B0 : BYTE;
    B1 : BYTE;
    B2 : BYTE;
    B3 : BYTE;
END_VAR
VAR
    lut : ARRAY[0..31] OF BYTE;
    i : INT;
END_VAR

D02_FRAME_CHECK := 0;
IF B0 = 16#A5 AND (B0 XOR B1 XOR B2) = B3 THEN
    i := BYTE_TO_INT(B1) - 200;
    IF i >= 0 THEN
        (* defect: B1 up to 255 gives i up to 55, lut has 32 entries *)
        D02_FRAME_CHECK := lut[i];
    END_IF;
END_IF;
END_FUNCTION
