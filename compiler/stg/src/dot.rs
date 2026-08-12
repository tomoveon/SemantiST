use crate::model::*;

pub fn render_pou(model: &PouSemanticModel) -> String {
    let mut output = String::new();
    output.push_str("digraph STG {\n");
    output.push_str("  rankdir=LR;\n");
    output.push_str("  graph [fontname=\"Helvetica\", label=\"");
    output.push_str(&escape(&model.pou.qualified_name));
    output.push_str("\", labelloc=t];\n");
    output.push_str("  node [fontname=\"Helvetica\", style=filled];\n");
    output.push_str("  edge [fontname=\"Helvetica\"];\n");

    for node in &model.graph.nodes {
        let (shape, color) = node_style(&node.kind);
        let mut label = node.label.clone();
        if let Some(outcome) = &node.outcome {
            label.push_str(&format!("\\n{:?}", outcome.role));
        }
        if let Some(source) = &node.source {
            label.push_str(&format!("\\n{}:{}", source.file, source.start_line));
        }
        output.push_str(&format!(
            "  \"{}\" [label=\"{}\", shape={}, fillcolor=\"{}\"];\n",
            escape(&node.id),
            escape(&label),
            shape,
            color
        ));
    }

    for edge in &model.graph.edges {
        let (color, style, prefix) = match edge.kind {
            EdgeKind::Evaluation => ("#2563eb", "solid", "eval"),
            EdgeKind::ControlTransfer => ("#475569", "solid", "ctrl"),
            EdgeKind::Temporal => ("#b45309", "dashed", "time"),
        };
        let mut label = prefix.to_string();
        if let Some(guard) = &edge.guard {
            label.push_str(&format!("\\n{}", short_id(guard)));
        }
        if let Some(summary) = &edge.transfer {
            label.push_str(&format!(
                "\\n{:?}, {} op",
                summary.modeling,
                summary.operations.len()
            ));
        }
        if edge.is_back_edge {
            label.push_str("\\nback");
        }
        output.push_str(&format!(
            "  \"{}\" -> \"{}\" [label=\"{}\", color=\"{}\", style={}];\n",
            escape(&edge.from),
            escape(&edge.to),
            escape(&label),
            color,
            style
        ));
    }
    output.push_str("}\n");
    output
}

fn node_style(kind: &NodeKind) -> (&'static str, &'static str) {
    match kind {
        NodeKind::Entry | NodeKind::Exit | NodeKind::CycleEntry | NodeKind::CycleExit => {
            ("oval", "#dbeafe")
        }
        NodeKind::Predicate(_) => ("diamond", "#fef3c7"),
        NodeKind::Outcome => ("box", "#dcfce7"),
        NodeKind::LoopLatch | NodeKind::LoopControl => ("hexagon", "#e0e7ff"),
        NodeKind::Hazard | NodeKind::PropertyViolation => ("octagon", "#fee2e2"),
    }
}

fn short_id(id: &str) -> &str {
    id.rsplit(':').next().unwrap_or(id)
}

fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
}
