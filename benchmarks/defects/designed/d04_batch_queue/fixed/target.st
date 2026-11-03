FUNCTION_BLOCK D04_BATCH_QUEUE
VAR_INPUT
    Enable : BOOL;
    Reset : BOOL;
    Write : BOOL;
    WriteValue : INT;
    Take : BOOL;
    StartDrain : BOOL;
    BatchId : INT;
    Ack : BOOL;
    AckBatch : INT;
END_VAR
VAR_OUTPUT
    Value : INT;
    Count : INT;
    Draining : BOOL;
END_VAR
VAR
    Buffer : ARRAY[0..3] OF INT;
    Head : INT;
    Tail : INT;
    ActiveBatch : INT;
    lastWrite : BOOL;
    lastTake : BOOL;
    lastStart : BOOL;
    lastAck : BOOL;
END_VAR

IF Reset THEN
    Head := 0;
    Tail := 0;
    Count := 0;
    Draining := FALSE;
ELSIF Enable THEN
    IF NOT Draining THEN
        IF Write AND NOT lastWrite AND Count < 4 THEN
            Buffer[Tail] := WriteValue;
            Tail := Tail + 1;
            IF Tail >= 4 THEN
                Tail := 0;
            END_IF;
            Count := Count + 1;
        END_IF;
        IF Take AND NOT lastTake AND Count > 0 THEN
            Value := Buffer[Head];
            Head := Head + 1;
            IF Head >= 4 THEN
                Head := 0;
            END_IF;
            Count := Count - 1;
        END_IF;
        IF StartDrain AND NOT lastStart AND Count > 0 AND BatchId > 100 THEN
            Draining := TRUE;
            ActiveBatch := BatchId;
        END_IF;
    ELSE
        IF Ack AND NOT lastAck AND AckBatch = ActiveBatch THEN
            Value := Buffer[Head];
            Head := Head + 1;
            IF Head >= 4 THEN
                Head := 0;
            END_IF;
            Count := Count - 1;
            IF Count = 0 THEN
                Draining := FALSE;
            END_IF;
        END_IF;
    END_IF;
END_IF;
lastWrite := Write;
lastTake := Take;
lastStart := StartDrain;
lastAck := Ack;
END_FUNCTION_BLOCK
