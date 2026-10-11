"""Verify real compiler-plan guidance reaches both branches from a zero seed.

Usage: python3 algorithm_guidance_check.py ENGINE PREPARED_TARGET NEW_RUN_DIR
PREPARED_TARGET contains the ALGORITHM_BRANCH build from algorithm_feedback.st.
"""
from pathlib import Path
import json
import os
import re
import subprocess
import sys


def main():
    engine, prepared, run = [Path(value).resolve() for value in sys.argv[1:4]]
    run.mkdir(parents=True, exist_ok=False)
    seeds = run / 'unified-corpus/seeds'
    seeds.mkdir(parents=True)
    (seeds / 'seed1').write_text('X,DINT,0\nY,DINT,0\n')
    env = {key: value for key, value in os.environ.items() if not key.startswith('SEMANTIST_')}
    env.update(SEMANTIST_SEMANTIC_TRACE_FILE=str(run / 'semantic-coverage.jsonl'),
               SEMANTIST_FB_MAX_CYCLES='256', SEMANTIST_FB_STALE_THRESHOLD='0')
    command = [str(engine), '--target', str(prepared / 'fuzz_target'),
               '--function', 'ALGORITHM_BRANCH', '--run-dir', str(run),
               '--semantic-task-plan', str(prepared / 'plan.json'),
               '--semantic-model', str(prepared / 'stg/stg-model.json'),
               '--semantic-runtime-ids', str(prepared / 'stg/stg-runtime-ids.json'),
               '--seed', '1001', '--fuzz-iterations', '150', '--timeout-ms', '1000']
    with (run / 'stdout.log').open('w') as log:
        subprocess.run(command, cwd=run, env=env, stdout=log,
                       stderr=subprocess.STDOUT, timeout=180, check=True)
    entries = json.loads((prepared / 'stg/stg-runtime-ids.json').read_text())['entries']
    expected = {item['stable_id'] for item in entries if item['kind'] == 'branch_outcome'}
    assert len(expected) == 4, expected
    events = [json.loads(line) for line in (run / 'semantic-coverage.jsonl').read_text().splitlines()]
    observed = {event['target_id'] for event in events if event.get('probe') == 'semantic_ir'}
    assert expected <= observed, {'missing': sorted(expected - observed)}
    witnesses = []
    for candidate in (run / 'unified-corpus/runtime').iterdir():
        if candidate.is_file() and not candidate.name.endswith('.metadata'):
            content = candidate.read_text(errors='replace')
            if re.search(r'(?m)^X,DINT,42$', content):
                witnesses.append(candidate.name)
                (run / 'x42-witness.seed').write_text(content)
                break
    assert witnesses, 'X=42 reached but no replayable corpus witness was preserved'
    report = {'passed': True, 'all_four_outcomes_observed': True,
              'initial_x': 0, 'initial_y': 0, 'x42_corpus_witness': witnesses[0],
              'seed': 1001, 'fuzz_iterations': 150, 'command': command}
    (run / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
