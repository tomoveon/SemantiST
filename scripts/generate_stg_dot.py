#!/usr/bin/env python3
"""Generate DOT graph visualizations from STG model JSON files."""

import json
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Color scheme ──────────────────────────────────────────────
COLORS = {
    'entry':          ('#1b5e20', '#c8e6c9', 'invhouse'),      # dark green
    'exit':           ('#b71c1c', '#ffcdd2', 'house'),          # dark red
    'cycle_entry':    ('#0d47a1', '#bbdefb', 'invhouse'),      # dark blue
    'cycle_exit':     ('#880e4f', '#f8bbd0', 'house'),         # dark pink
    'predicate':      ('#e65100', '#ffe0b2', 'diamond'),       # orange
    'outcome_true':   ('#2e7d32', '#c8e6c9', 'ellipse'),       # green
    'outcome_false':  ('#c62828', '#ffcdd2', 'ellipse'),       # red
    'outcome_case':   ('#6a1b9a', '#e1bee7', 'ellipse'),       # purple
    'outcome_default':('#4e342e', '#d7ccc8', 'ellipse'),       # brown
    'outcome_loop':   ('#00695c', '#b2dfdb', 'ellipse'),       # teal
    'outcome_other':  ('#37474f', '#cfd8dc', 'ellipse'),       # grey
    'hazard_reach':   ('#bf360c', '#ffccbc', 'octagon'),       # deep orange
    'hazard_viol':    ('#b71c1c', '#ff8a80', 'doubleoctagon'), # red
    'hazard_ext':     ('#f57f17', '#fff9c4', 'octagon'),       # yellow
    'loop_latch':     ('#546e7a', '#cfd8dc', 'component'),     # blue-grey
    'loop_control':   ('#546e7a', '#cfd8dc', 'component'),     # blue-grey
    'property_viol':  ('#880e4f', '#f8bbd0', 'doubleoctagon'), # pink
}

EDGE_COLORS = {
    'evaluation':       ('#616161', 'dashed'),
    'control_transfer': ('#424242', 'solid'),
    'temporal':         ('#d32f2f', 'bold'),
}


def short_id(node_id):
    """Return a readable short ID from a SHA-prefixed ID."""
    if ':' in node_id:
        prefix, rest = node_id.split(':', 1)
        return f"{prefix}:{rest[:8]}"
    return node_id[:12]


def node_style(node):
    """Determine color scheme for a node based on its kind, outcome role, and hazard type."""
    kind = node.get('kind', {})
    category = kind.get('category', '') if isinstance(kind, dict) else str(kind)

    if category == 'entry':
        return COLORS['entry']
    if category == 'exit':
        return COLORS['exit']
    if category == 'cycle_entry':
        return COLORS['cycle_entry']
    if category == 'cycle_exit':
        return COLORS['cycle_exit']

    if category == 'predicate':
        return COLORS['predicate']

    if category == 'outcome':
        outcome = node.get('outcome', {})
        role = outcome.get('role', '')
        if role == 'true':
            return COLORS['outcome_true']
        if role == 'false':
            return COLORS['outcome_false']
        if role in ('case_label', 'case_default'):
            if role == 'case_default':
                return COLORS['outcome_default']
            return COLORS['outcome_case']
        if role in ('loop_enter', 'loop_continue', 'loop_exit', 'loop_back'):
            return COLORS['outcome_loop']
        return COLORS['outcome_other']

    if category == 'hazard':
        hazard = node.get('hazard', {})
        hkind = hazard.get('kind', '')
        if hkind == 'external_output':
            return COLORS['hazard_ext']
        # For hazard nodes, check the targets to distinguish reach vs violation
        return COLORS['hazard_reach']

    if category == 'loop_latch':
        return COLORS['loop_latch']
    if category == 'loop_control':
        return COLORS['loop_control']
    if category == 'property_violation':
        return COLORS['property_viol']

    return ('#333333', '#eeeeee', 'ellipse')


def node_label(node, targets_by_node):
    """Build a rich label for a node."""
    kind = node.get('kind', {})
    category = kind.get('category', '') if isinstance(kind, dict) else str(kind)
    label = node.get('label', '?')

    lines = [label]

    if category == 'predicate':
        pk = kind.get('kind', '?') if isinstance(kind, dict) else ''
        lines[0] = f"{pk.upper()}"

    if category == 'outcome':
        outcome = node.get('outcome', {})
        role = outcome.get('role', '?')
        lines[0] = role.upper()

    if category == 'hazard':
        hazard = node.get('hazard', {})
        hk = hazard.get('kind', '?')
        detail = hazard.get('detail', '')
        lines[0] = hk.replace('_', '\n')
        if detail and len(detail) < 40:
            lines.append(detail[:40])

    # Show source location if available
    source = node.get('source')
    if source:
        lines.append(f"L{source['start_line']}-{source['end_line']}")

    return '\\n'.join(lines)


def build_graph(pou):
    """Build structured graph data from a POU model."""
    graph = pou['graph']

    node_map = {n['id']: n for n in graph['nodes']}

    # Build target -> node mapping
    targets_by_node = {}
    for t in pou.get('targets', []):
        nid = t['node_id']
        if nid not in targets_by_node:
            targets_by_node[nid] = []
        targets_by_node[nid].append(t)

    eval_edges = [e for e in graph['edges'] if e['kind'] == 'evaluation']
    ctrl_edges = [e for e in graph['edges'] if e['kind'] == 'control_transfer']
    temp_edges = [e for e in graph['edges'] if e['kind'] == 'temporal']

    return {
        'nodes': graph['nodes'],
        'eval_edges': eval_edges,
        'ctrl_edges': ctrl_edges,
        'temp_edges': temp_edges,
        'node_map': node_map,
        'targets_by_node': targets_by_node,
        'entry_id': graph.get('entry_id'),
        'exit_id': graph.get('exit_id'),
        'cycle_entry_id': graph.get('cycle_entry_id'),
        'cycle_exit_id': graph.get('cycle_exit_id'),
    }


def write_dot(pou, g, output_path):
    """Write a DOT file for the STG."""
    pou_info = pou['pou']
    name = pou_info['qualified_name']
    stateful = pou_info.get('stateful', False)

    dot = []
    dot.append('digraph STG {')
    dot.append(f'  label="{name} — STG Semantic Branch Graph";')
    dot.append('  labelloc="t";')
    dot.append('  fontsize=20;')
    dot.append('  fontname="Helvetica";')
    dot.append('  rankdir=TB;')
    dot.append('  splines=ortho;')
    dot.append('  nodesep=0.6;')
    dot.append('  ranksep=0.8;')
    dot.append('  bgcolor="#fafafa";')
    dot.append('  pad=0.5;')
    dot.append('')

    # Legend
    dot.append('  subgraph cluster_legend {')
    dot.append('    label="Legend";')
    dot.append('    fontsize=11;')
    dot.append('    style="rounded,dashed";')
    dot.append('    bgcolor="#ffffff";')
    dot.append('    color="#999999";')
    dot.append('    legend_entry [label="Entry/Exit" shape=invhouse style=filled fillcolor="#c8e6c9" fontsize=10];')
    if stateful:
        dot.append('    legend_cycle [label="CycleEntry/CycleExit" shape=invhouse style=filled fillcolor="#bbdefb" fontsize=10];')
    dot.append('    legend_pred [label="Predicate" shape=diamond style=filled fillcolor="#ffe0b2" fontsize=10];')
    dot.append('    legend_true [label="True" shape=ellipse style=filled fillcolor="#c8e6c9" fontsize=10];')
    dot.append('    legend_false [label="False" shape=ellipse style=filled fillcolor="#ffcdd2" fontsize=10];')
    dot.append('    legend_hazard [label="Hazard" shape=octagon style=filled fillcolor="#ffccbc" fontsize=10];')
    dot.append('    legend_eval [label="Evaluation" color="#616161" style=dashed fontsize=10];')
    dot.append('    legend_ctrl [label="Control" color="#424242" style=solid fontsize=10];')
    if stateful:
        dot.append('    legend_temp [label="Temporal" color="#d32f2f" style=bold fontsize=10];')
    dot.append('  }')
    dot.append('')

    # Nodes
    node_map = g['node_map']
    targets_by_node = g['targets_by_node']

    # Group nodes by rank layers
    structural_ids = set()
    predicate_ids = set()
    outcome_ids = set()
    hazard_ids = set()
    other_ids = set()

    for node in g['nodes']:
        nid = node['id']
        kind = node.get('kind', {})
        category = kind.get('category', '') if isinstance(kind, dict) else str(kind)

        if category in ('entry', 'exit', 'cycle_entry', 'cycle_exit'):
            structural_ids.add(nid)
        elif category == 'predicate':
            predicate_ids.add(nid)
        elif category == 'outcome':
            outcome_ids.add(nid)
        elif category == 'hazard':
            hazard_ids.add(nid)
        else:
            other_ids.add(nid)

    # Write structural nodes
    if structural_ids:
        dot.append('  // ═══ Structural nodes ═══')
        dot.append('  { rank=min;')
        for nid in sorted(structural_ids):
            if nid == g['entry_id'] or (g.get('cycle_entry_id') and nid == g['cycle_entry_id']):
                dot.append(f'    "{nid}";')
        dot.append('  }')
        dot.append('  { rank=max;')
        for nid in sorted(structural_ids):
            if nid == g['exit_id'] or (g.get('cycle_exit_id') and nid == g['cycle_exit_id']):
                dot.append(f'    "{nid}";')
        dot.append('  }')

    dot.append('')
    for node in g['nodes']:
        nid = node['id']
        sid = short_id(nid)
        fg, bg, shape = node_style(node)
        label = node_label(node, targets_by_node)

        # Escape quotes in label
        label = label.replace('"', '\\"')

        dot.append(f'  "{nid}" [')
        dot.append(f'    label="{label}\\n[{sid}]";')
        dot.append(f'    shape={shape};')
        dot.append(f'    style=filled;')
        dot.append(f'    fillcolor="{bg}";')
        dot.append(f'    color="{fg}";')
        dot.append(f'    fontcolor="{fg}";')
        dot.append(f'    fontsize=9;')
        dot.append(f'    fontname="Helvetica";')
        dot.append(f'    penwidth=2;')
        dot.append(f'    margin="0.12,0.08";')
        dot.append(f'  ];')

    dot.append('')

    # Edges
    dot.append('  // ═══ Evaluation edges ═══')
    for edge in g['eval_edges']:
        fg, style = EDGE_COLORS['evaluation']
        guard = edge.get('guard', '')
        guard_short = short_id(guard) if guard else ''
        label = f'guard={guard_short}' if guard_short else ''
        label = label.replace('"', '\\"')
        dot.append(f'  "{edge["from"]}" -> "{edge["to"]}" [')
        dot.append(f'    label="{label}";')
        dot.append(f'    color="{fg}";')
        dot.append(f'    style={style};')
        dot.append(f'    fontsize=7;')
        dot.append(f'    fontcolor="#757575";')
        dot.append(f'    arrowsize=0.6;')
        if edge.get('is_back_edge'):
            dot.append(f'    constraint=false;')
            dot.append(f'    color="#ff6f00";')
        dot.append(f'  ];')

    dot.append('')
    dot.append('  // ═══ Control-transfer edges ═══')
    for edge in g['ctrl_edges']:
        fg, style = EDGE_COLORS['control_transfer']
        summary = edge.get('transfer', {})
        ops = summary.get('operations', [])
        reads = summary.get('reads', [])
        writes = summary.get('writes', [])
        modeling = summary.get('modeling', 'exact')

        parts = []
        if ops:
            op_kinds = []
            for op in ops:
                ok = op.get('kind', '?')
                op_kinds.append(ok)
            parts.append(','.join(op_kinds[:3]))
        if reads:
            parts.append(f'R:{len(reads)}')
        if writes:
            parts.append(f'W:{len(writes)}')
        if modeling != 'exact':
            parts.append(modeling[:4])

        label = ' '.join(parts)
        label = label.replace('"', '\\"')
        edge_color = fg
        if modeling == 'conservative':
            edge_color = '#e65100'
        elif modeling == 'opaque':
            edge_color = '#b71c1c'

        dot.append(f'  "{edge["from"]}" -> "{edge["to"]}" [')
        dot.append(f'    label="{label}";')
        dot.append(f'    color="{edge_color}";')
        dot.append(f'    style={style};')
        dot.append(f'    fontsize=7;')
        dot.append(f'    fontcolor="#616161";')
        dot.append(f'    penwidth=1.5;')
        if edge.get('is_back_edge'):
            dot.append(f'    constraint=false;')
            dot.append(f'    color="#ff6f00";')
        dot.append(f'  ];')

    if g['temp_edges']:
        dot.append('')
        dot.append('  // ═══ Temporal edges ═══')
        for edge in g['temp_edges']:
            fg, style = EDGE_COLORS['temporal']
            temporal = edge.get('temporal', {})
            symbols = temporal.get('persistent_symbols', [])
            label = f'state:{len(symbols)} symbols'
            label = label.replace('"', '\\"')
            dot.append(f'  "{edge["from"]}" -> "{edge["to"]}" [')
            dot.append(f'    label="{label}";')
            dot.append(f'    color="{fg}";')
            dot.append(f'    style={style};')
            dot.append(f'    fontsize=7;')
            dot.append(f'    fontcolor="#d32f2f";')
            dot.append(f'    penwidth=2.5;')
            dot.append(f'    constraint=false;')
            dot.append(f'  ];')

    dot.append('}')

    with open(output_path, 'w') as f:
        f.write('\n'.join(dot))

    return '\n'.join(dot)


def main():
    targets = [
        ('days_in_month', 'FUNCTION'),
        ('fifo_16', 'FUNCTION_BLOCK'),
    ]

    for name, ftype in targets:
        model_path = ROOT / 'artifacts' / 'benchmarks' / 'oscat_basic_5m' / name / 'stg' / 'stg-model.json'
        if not os.path.exists(model_path):
            print(f"SKIP {name}: no stg-model.json")
            continue

        with open(model_path) as f:
            model = json.load(f)

        for pou in model['pous']:
            pou_name = pou['pou']['qualified_name']
            output_path = ROOT / 'artifacts' / 'benchmarks' / 'oscat_basic_5m' / name / 'stg' / f'{pou_name}_stg.dot'

            g = build_graph(pou)
            write_dot(pou, g, output_path)
            print(f"Wrote: {output_path}")
            print(f"  {len(g['nodes'])} nodes, {len(g['eval_edges'])} eval edges, "
                  f"{len(g['ctrl_edges'])} ctrl edges, {len(g['temp_edges'])} temporal edges")


if __name__ == '__main__':
    main()
