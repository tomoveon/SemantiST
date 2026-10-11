"""Check the actual ST ALGORITHM_STATEFUL target built by validation tooling.

Usage: python3 tests/e2e/algorithm_telemetry_check.py TARGET RUNTIME_IDS
The fixture has a private counter, enabled each scan, and a true branch at 100.
No runtime/state text trace is enabled.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


def main() -> None:
    target, ids = map(Path, sys.argv[1:3])
    entries = json.loads(ids.read_text())["entries"]
    size = max(entry["runtime_id"] for entry in entries) + 1
    telemetry_size = 16 + 4096 * 8 + 512 * 16
    libc = ctypes.CDLL(None)
    libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    libc.shmget.restype = ctypes.c_int
    libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.shmat.restype = ctypes.c_void_p
    libc.shmdt.argtypes = [ctypes.c_void_p]
    libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    memories = []
    try:
        for length in [size, telemetry_size]:
            shmid = libc.shmget(0, length, 0o1000 | 0o600)
            assert shmid >= 0
            pointer = libc.shmat(shmid, None, 0)
            assert pointer != ctypes.c_void_p(-1).value
            memories.append((shmid, pointer, length))
        env = os.environ.copy()
        for key in ["SEMANTIST_SEMANTIC_TRACE_RUNTIME", "SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE",
                    "SEMANTIST_FB_STATE_TRACE", "SEMANTIST_FB_STATE_TRACE_FILE"]:
            env.pop(key, None)
        env.update(SEMANTIST_SEMANTIC_SHM_ID=str(memories[0][0]),
                   SEMANTIST_SEMANTIC_MAP_SIZE=str(size),
                   SEMANTIST_TELEMETRY_SHM_ID=str(memories[1][0]))

        def run(cycles: int, limit: int = 256, enabled: bool = True):
            for _, pointer, length in memories:
                ctypes.memset(pointer, 0, length)
            seed = ''.join(f'{i}.enable,BOOL,{int(enabled)}\n{i}.reset,BOOL,0\n' for i in range(cycles))
            result = subprocess.run([str(target)], input=seed.encode(), env={**env, 'SEMANTIST_FB_MAX_CYCLES': str(limit)}, capture_output=True)
            assert result.returncode == 0, result.stderr.decode(errors='replace')
            bitmap = ctypes.string_at(memories[0][1], size)
            raw = ctypes.string_at(memories[1][1], telemetry_size)
            scans, events, states, flags = struct.unpack_from('=IIII', raw)
            assert scans == min(cycles, limit), (cycles, scans)
            ordered = [struct.unpack_from('=II', raw, 16 + index * 8) for index in range(events)]
            assert all(cycle < scans for _, cycle in ordered)
            assert all(left[1] <= right[1] for left, right in zip(ordered, ordered[1:]))
            assert flags & 4
            assert states == min(scans, 512)
            return bitmap, ordered, raw, (scans, events, states, flags)

        # Locate the deep predicate from the fixture, not a guessed runtime ID.
        deep = [entry['runtime_id'] for entry in entries if entry['pou'] == 'ALGORITHM_STATEFUL'
                and entry['kind'] == 'branch_outcome' and entry.get('role') == 'true'
                and entry.get('source', {}).get('start_line') == 31]
        assert len(deep) == 1, deep
        before = run(99)
        after = run(100)
        assert not before[0][deep[0]]
        assert after[0][deep[0]]
        assert (deep[0], 99) in after[1]
        assert not after[3][3] & 3
        short = run(2)
        idle = run(2, enabled=False)
        state_offset = 16 + 4096 * 8
        assert short[2][state_offset:state_offset + 32] != idle[2][state_offset:state_offset + 32]
        capped = run(300)
        overflow = run(1000, limit=1000)
        assert overflow[3][3] & 1 and overflow[3][3] & 2
        # A later short input has no tail/overflow evidence from the old input.
        reset = run(2)
        assert reset[3] == short[3]
        print(json.dumps({'passed': True, 'deep_target_runtime_id': deep[0],
                          'cycle_99': before[3], 'cycle_100': after[3],
                          'limit_256': capped[3], 'overflow_1000': overflow[3],
                          'subsequent_short': reset[3], 'text_traces_enabled': False}, indent=2))
    finally:
        for shmid, pointer, _ in memories:
            libc.shmdt(pointer)
            libc.shmctl(shmid, 0, None)


if __name__ == '__main__':
    main()
