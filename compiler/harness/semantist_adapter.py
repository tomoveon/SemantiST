"""SemantiST-only sequence adapter.

This is the boundary between SemantiST's planning protocol and the neutral
Common PLC Harness.  LLM/KLEE plans may use ``0.field`` records and multiple
scan cycles here; each selected cycle is converted to one fixed-size raw ABI
record before execution.  Baseline drivers never import this module.
"""

from __future__ import annotations

import re
import struct
from typing import Iterable

from compiler.harness.abi import TargetAbi, TypeSpec
from compiler.harness.common import input_layout
from fuzzer.corpus.frames import TestCase, parse_testcase


def _integer(value: str) -> int:
    text = value.strip()
    return int(text, 16) if re.fullmatch(r"[+-]?0[xX][0-9a-fA-F]+", text) else int(text, 10)


def _bytes_for_spec(spec: TypeSpec, value: str) -> bytes:
    text = value.strip()
    if spec.kind == "scalar":
        if text.startswith(("0x", "0X")) and len(text[2:]) == spec.size * 2:
            return bytes.fromhex(text[2:])
        if spec.name in ("REAL", "LREAL"):
            return struct.pack("<f" if spec.size == 4 else "<d", float(text))
        signed = spec.c_type.startswith("int")
        return int(_integer(text)).to_bytes(spec.size, "little", signed=signed)
    if spec.kind == "string":
        if text.startswith(("0x", "0X")):
            payload = bytes.fromhex(text[2:])
            if len(payload) > spec.byte_size() or len(payload) % spec.size:
                raise ValueError(f"{spec.name} raw storage exceeds capacity or splits a code unit")
            # Raw strings are complete ABI objects. A full N+1 byte value may
            # intentionally omit its terminator.
            return payload + bytes(spec.byte_size() - len(payload))
        else:
            payload = text.encode("utf-16le" if spec.size == 2 else "utf-8")
        capacity = max(0, spec.byte_size() - spec.size)
        if len(payload) > capacity or len(payload) % spec.size:
            raise ValueError(f"{spec.name} payload exceeds capacity or splits a code unit")
        return payload + bytes(spec.byte_size() - len(payload))
    if text.startswith(("0x", "0X")):
        payload = bytes.fromhex(text[2:])
    else:
        payload = text.encode("utf-8")
    if len(payload) > spec.byte_size():
        raise ValueError(f"{spec.name} payload exceeds capacity")
    return payload + bytes(spec.byte_size() - len(payload))


def testcase_to_raw(testcase: TestCase, abi: TargetAbi, frame_index: int = 0) -> bytes:
    """Encode one canonical SemantiST frame as a Common Harness record."""
    size, _align, fields = input_layout(abi)
    if not testcase.frames:
        raise ValueError("SemantiST testcase has no frames")
    if frame_index < 0 or frame_index >= len(testcase.frames):
        raise IndexError(frame_index)
    frame = testcase.frames[frame_index]
    values = dict(testcase.shared)
    values.update(frame.values)
    output = bytearray(size)
    for field in fields:
        value = values.get(field.param.name)
        if value is None:
            continue
        if field.param.spec.kind == "pointer":
            text = value.strip()
            payload = (bytes.fromhex(text[2:]) if text.startswith(("0x", "0X"))
                       else text.encode("utf-8"))
            if len(payload) > field.size:
                raise ValueError(f"{field.param.name} payload exceeds capacity")
            encoded = payload + bytes(field.size - len(payload))
        else:
            encoded = _bytes_for_spec(field.param.spec, value)
        output[field.offset:field.offset + field.size] = encoded[:field.size]
    return bytes(output)


def text_to_raw_sequence(text: str | bytes, abi: TargetAbi) -> list[bytes]:
    """Parse SemantiST text and return one raw record per planned cycle."""
    testcase = parse_testcase(text, abi)
    return [testcase_to_raw(testcase, abi, index)
            for index in range(len(testcase.frames))]


def plan_to_raw_sequence(records: Iterable[str | bytes], abi: TargetAbi) -> list[bytes]:
    """Convert an iterable of LLM/KLEE frame plans to raw ABI records."""
    result: list[bytes] = []
    for record in records:
        result.extend(text_to_raw_sequence(record, abi))
    return result
