use std::collections::{BTreeMap, BTreeSet};

use crate::extract::recompute_statistics;
use crate::model::*;

pub fn validate_project(model: &mut ProjectSemanticModel) {
    for pou in &mut model.pous {
        validate_pou(pou);
        pou.statistics = statistics(pou);
    }
    model.diagnostics = model
        .pous
        .iter()
        .flat_map(|pou| pou.diagnostics.iter().cloned())
        .collect();
    recompute_statistics(model);
}

fn validate_pou(pou: &mut PouSemanticModel) {
    let node_ids = pou
        .graph
        .nodes
        .iter()
        .map(|node| node.id.clone())
        .collect::<BTreeSet<_>>();
    let expression_ids = pou
        .environment
        .expressions
        .iter()
        .map(|expression| expression.id.clone())
        .collect::<BTreeSet<_>>();
    let type_names = pou
        .environment
        .types
        .iter()
        .map(|ty| ty.name.to_ascii_lowercase())
        .chain(["void".to_string()])
        .collect::<BTreeSet<_>>();
    let mapping_ids = pou
        .mappings
        .nodes
        .iter()
        .map(|mapping| mapping.semantic_id.clone())
        .collect::<BTreeSet<_>>();
    let edge_ids = pou
        .graph
        .edges
        .iter()
        .map(|edge| edge.id.clone())
        .collect::<BTreeSet<_>>();

    check_unique(
        &mut pou.diagnostics,
        pou.graph.nodes.iter().map(|node| &node.id),
        "STG-E001",
        "duplicate node semantic ID",
        &pou.pou.qualified_name,
    );
    check_unique(
        &mut pou.diagnostics,
        pou.graph.edges.iter().map(|edge| &edge.id),
        "STG-E002",
        "duplicate edge semantic ID",
        &pou.pou.qualified_name,
    );
    check_unique(
        &mut pou.diagnostics,
        pou.targets.iter().map(|target| &target.id),
        "STG-E003",
        "duplicate target semantic ID",
        &pou.pou.qualified_name,
    );

    for edge in &pou.graph.edges {
        if !node_ids.contains(&edge.from) || !node_ids.contains(&edge.to) {
            diagnostic(
                &mut pou.diagnostics,
                "STG-E004",
                DiagnosticSeverity::Error,
                "edge has a dangling node reference",
                &pou.pou.qualified_name,
                Some(edge.id.clone()),
                None,
            );
        }
        match edge.kind {
            EdgeKind::Evaluation => {
                if edge
                    .guard
                    .as_ref()
                    .is_none_or(|guard| !expression_ids.contains(guard))
                {
                    diagnostic(
                        &mut pou.diagnostics,
                        "STG-E005",
                        DiagnosticSeverity::Error,
                        "evaluation edge has no valid typed guard",
                        &pou.pou.qualified_name,
                        Some(edge.id.clone()),
                        None,
                    );
                }
            }
            EdgeKind::ControlTransfer => {
                if edge.transfer.is_none() {
                    diagnostic(
                        &mut pou.diagnostics,
                        "STG-E006",
                        DiagnosticSeverity::Error,
                        "control-transfer edge has no transfer summary",
                        &pou.pou.qualified_name,
                        Some(edge.id.clone()),
                        None,
                    );
                }
            }
            EdgeKind::Temporal => {
                if edge.temporal.is_none() {
                    diagnostic(
                        &mut pou.diagnostics,
                        "STG-E007",
                        DiagnosticSeverity::Error,
                        "temporal edge has no state summary",
                        &pou.pou.qualified_name,
                        Some(edge.id.clone()),
                        None,
                    );
                }
            }
        }
    }

    validate_predicates(pou);
    validate_loops(pou);
    validate_state(pou);

    for expression in &pou.environment.expressions {
        for operand in &expression.operands {
            if !expression_ids.contains(operand) {
                diagnostic(
                    &mut pou.diagnostics,
                    "STG-E020",
                    DiagnosticSeverity::Error,
                    "expression DAG has a dangling operand",
                    &pou.pou.qualified_name,
                    Some(expression.id.clone()),
                    expression.source.clone(),
                );
            }
        }
        if !type_names.contains(&expression.type_name.to_ascii_lowercase())
            && expression.type_name != "VOID"
        {
            diagnostic(
                &mut pou.diagnostics,
                "STG-W021",
                DiagnosticSeverity::Warning,
                "expression type is not materialized in the POU type environment",
                &pou.pou.qualified_name,
                Some(expression.id.clone()),
                expression.source.clone(),
            );
        }
    }

    for edge in &pou.graph.edges {
        let Some(summary) = &edge.transfer else {
            continue;
        };
        for operation in &summary.operations {
            if let TransferOperation::Assign {
                target,
                value,
                conversion,
                source,
                ..
            } = operation
            {
                let left = pou
                    .environment
                    .expressions
                    .iter()
                    .find(|expression| &expression.id == target);
                let right = pou
                    .environment
                    .expressions
                    .iter()
                    .find(|expression| &expression.id == value);
                match (left, right) {
                    (Some(left), Some(right))
                        if left.type_name != right.type_name && conversion.is_none() =>
                    {
                        diagnostic(
                            &mut pou.diagnostics,
                            "STG-E023",
                            DiagnosticSeverity::Error,
                            "typed assignment changes type without an explicit conversion record",
                            &pou.pou.qualified_name,
                            Some(edge.id.clone()),
                            Some(source.clone()),
                        );
                    }
                    (Some(_), Some(_)) => {}
                    _ => {
                        diagnostic(
                            &mut pou.diagnostics,
                            "STG-E022",
                            DiagnosticSeverity::Error,
                            "assignment references an unknown expression",
                            &pou.pou.qualified_name,
                            Some(edge.id.clone()),
                            Some(source.clone()),
                        );
                    }
                }
            }
        }
    }

    for target in &pou.targets {
        if !node_ids.contains(&target.node_id) || !mapping_ids.contains(&target.node_id) {
            diagnostic(
                &mut pou.diagnostics,
                "STG-E030",
                DiagnosticSeverity::Error,
                "target has no valid node and source-AST mapping",
                &pou.pou.qualified_name,
                Some(target.id.clone()),
                target.source.clone(),
            );
        }
        if target.edge_ids.is_empty()
            || target
                .edge_ids
                .iter()
                .any(|edge_id| !edge_ids.contains(edge_id))
        {
            diagnostic(
                &mut pou.diagnostics,
                "STG-E032",
                DiagnosticSeverity::Error,
                "target has no valid semantic-edge mapping",
                &pou.pou.qualified_name,
                Some(target.id.clone()),
                target.source.clone(),
            );
        }
        if target.source.is_none()
            && !matches!(target.kind, TargetKind::LoopOutcome | TargetKind::Cycle)
        {
            diagnostic(
                &mut pou.diagnostics,
                "STG-W031",
                DiagnosticSeverity::Warning,
                "target is synthetic and has no direct source span",
                &pou.pou.qualified_name,
                Some(target.id.clone()),
                None,
            );
        }
    }
}

fn validate_predicates(pou: &mut PouSemanticModel) {
    let outcomes = pou
        .graph
        .nodes
        .iter()
        .filter_map(|node| {
            node.outcome
                .as_ref()
                .map(|outcome| (node.id.clone(), outcome))
        })
        .fold(
            BTreeMap::<String, Vec<(String, OutcomeRole)>>::new(),
            |mut map, (node, outcome)| {
                if let Some(predicate) = &outcome.predicate_id {
                    map.entry(predicate.clone())
                        .or_default()
                        .push((node, outcome.role.clone()));
                }
                map
            },
        );
    let evaluation_pairs = pou
        .graph
        .edges
        .iter()
        .filter(|edge| edge.kind == EdgeKind::Evaluation)
        .map(|edge| (edge.from.clone(), edge.to.clone()))
        .collect::<BTreeSet<_>>();
    let predicates = pou
        .graph
        .nodes
        .iter()
        .filter_map(|node| match &node.kind {
            NodeKind::Predicate(kind) => Some((node.id.clone(), kind.clone(), node.source.clone())),
            _ => None,
        })
        .collect::<Vec<_>>();

    for (predicate_id, kind, source) in predicates {
        let predicate_outcomes = outcomes.get(&predicate_id).cloned().unwrap_or_default();
        let roles = predicate_outcomes
            .iter()
            .map(|(_, role)| role.clone())
            .collect::<Vec<_>>();
        let complete = match kind {
            PredicateKind::If | PredicateKind::Elsif => {
                roles.contains(&OutcomeRole::True)
                    && (roles.contains(&OutcomeRole::False) || roles.contains(&OutcomeRole::Else))
            }
            PredicateKind::Case => {
                roles.contains(&OutcomeRole::CaseLabel) && roles.contains(&OutcomeRole::CaseDefault)
            }
            PredicateKind::While | PredicateKind::For => {
                roles.contains(&OutcomeRole::LoopEnter) && roles.contains(&OutcomeRole::LoopExit)
            }
            PredicateKind::Repeat => {
                roles.contains(&OutcomeRole::LoopContinue) && roles.contains(&OutcomeRole::LoopExit)
            }
        };
        if !complete {
            diagnostic(
                &mut pou.diagnostics,
                "STG-E040",
                DiagnosticSeverity::Error,
                "predicate does not have a complete outcome partition",
                &pou.pou.qualified_name,
                Some(predicate_id.clone()),
                source.clone(),
            );
        }
        for (outcome_id, _) in predicate_outcomes {
            if !evaluation_pairs.contains(&(predicate_id.clone(), outcome_id)) {
                diagnostic(
                    &mut pou.diagnostics,
                    "STG-E041",
                    DiagnosticSeverity::Error,
                    "predicate outcome has no evaluation edge",
                    &pou.pou.qualified_name,
                    Some(predicate_id.clone()),
                    source.clone(),
                );
            }
        }
    }
}

fn validate_loops(pou: &mut PouSemanticModel) {
    let loops = pou
        .graph
        .nodes
        .iter()
        .filter_map(|node| match node.kind {
            NodeKind::Predicate(
                PredicateKind::While | PredicateKind::Repeat | PredicateKind::For,
            ) => Some((node.id.clone(), node.source.clone())),
            _ => None,
        })
        .collect::<Vec<_>>();
    for (loop_id, source) in loops {
        let has_back_edge = pou.graph.edges.iter().any(|edge| {
            edge.is_back_edge
                && (edge.to == loop_id
                    || pou.graph.nodes.iter().any(|node| {
                        node.id == edge.to && node.features.loop_id.as_deref() == Some(&loop_id)
                    }))
        });
        let has_exit = pou.graph.nodes.iter().any(|node| {
            node.outcome.as_ref().is_some_and(|outcome| {
                outcome.predicate_id.as_deref() == Some(&loop_id)
                    && outcome.role == OutcomeRole::LoopExit
            })
        });
        let has_back_target = pou.targets.iter().any(|target| {
            target.role == Some(OutcomeRole::LoopBack)
                && pou.graph.nodes.iter().any(|node| {
                    node.id == target.node_id && node.features.loop_id.as_deref() == Some(&loop_id)
                })
        });
        if !has_back_edge || !has_exit || !has_back_target {
            diagnostic(
                &mut pou.diagnostics,
                "STG-E050",
                DiagnosticSeverity::Error,
                "loop is missing a real back edge or exit outcome",
                &pou.pou.qualified_name,
                Some(loop_id),
                source,
            );
        }
    }
}

fn validate_state(pou: &mut PouSemanticModel) {
    if !pou.pou.stateful {
        return;
    }
    let Some(cycle_entry) = &pou.graph.cycle_entry_id else {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E060",
            DiagnosticSeverity::Error,
            "stateful POU has no CycleEntry",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
        return;
    };
    let Some(cycle_exit) = &pou.graph.cycle_exit_id else {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E061",
            DiagnosticSeverity::Error,
            "stateful POU has no CycleExit",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
        return;
    };
    let temporal = pou.graph.edges.iter().find(|edge| {
        edge.kind == EdgeKind::Temporal && &edge.from == cycle_exit && &edge.to == cycle_entry
    });
    let Some(state) = pou.environment.persistent_state.as_ref() else {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E062",
            DiagnosticSeverity::Error,
            "stateful POU has no valid cross-cycle persistent-state relation",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
        return;
    };
    let Some(temporal) = temporal.and_then(|edge| edge.temporal.as_ref()) else {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E062",
            DiagnosticSeverity::Error,
            "stateful POU has no valid cross-cycle persistent-state relation",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
        return;
    };
    let carried = state.symbols.iter().cloned().collect::<BTreeSet<_>>();
    let refreshed = state
        .cycle_inputs
        .iter()
        .chain(&state.inout_symbols)
        .chain(&state.configuration_symbols)
        .cloned()
        .collect::<BTreeSet<_>>();
    if !carried.is_disjoint(&refreshed) {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E063",
            DiagnosticSeverity::Error,
            "persistent state overlaps cycle inputs, in-out symbols, or configuration inputs",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
    }
    if carried
        != temporal
            .persistent_symbols
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
    {
        diagnostic(
            &mut pou.diagnostics,
            "STG-E064",
            DiagnosticSeverity::Error,
            "temporal edge does not carry exactly the persistent-state symbols",
            &pou.pou.qualified_name,
            None,
            Some(pou.pou.source.clone()),
        );
    }
}

fn check_unique<'a>(
    diagnostics: &mut Vec<ModelDiagnostic>,
    ids: impl Iterator<Item = &'a String>,
    code: &str,
    message: &str,
    pou: &str,
) {
    let mut seen = BTreeSet::new();
    for id in ids {
        if !seen.insert(id) {
            diagnostic(
                diagnostics,
                code,
                DiagnosticSeverity::Error,
                message,
                pou,
                Some(id.clone()),
                None,
            );
        }
    }
}

fn diagnostic(
    diagnostics: &mut Vec<ModelDiagnostic>,
    code: &str,
    severity: DiagnosticSeverity,
    message: &str,
    pou: &str,
    semantic_id: Option<String>,
    source: Option<SourceSpan>,
) {
    diagnostics.push(ModelDiagnostic {
        code: code.to_string(),
        severity,
        message: message.to_string(),
        pou: Some(pou.to_string()),
        semantic_id,
        source,
    });
}

fn statistics(model: &PouSemanticModel) -> ModelStatistics {
    ModelStatistics {
        pou_count: 1,
        node_count: model.graph.nodes.len(),
        edge_count: model.graph.edges.len(),
        target_count: model.targets.len(),
        predicate_count: model
            .graph
            .nodes
            .iter()
            .filter(|node| matches!(node.kind, NodeKind::Predicate(_)))
            .count(),
        loop_count: model
            .graph
            .nodes
            .iter()
            .filter(|node| {
                matches!(
                    node.kind,
                    NodeKind::Predicate(
                        PredicateKind::While | PredicateKind::Repeat | PredicateKind::For
                    )
                )
            })
            .count(),
        persistent_state_count: model
            .environment
            .persistent_state
            .as_ref()
            .map(|state| state.symbols.len())
            .unwrap_or(0),
        conservative_summary_count: model
            .graph
            .edges
            .iter()
            .filter(|edge| {
                edge.transfer
                    .as_ref()
                    .is_some_and(|summary| summary.modeling == ModelingStatus::Conservative)
            })
            .count(),
        opaque_summary_count: model
            .graph
            .edges
            .iter()
            .filter(|edge| {
                edge.transfer
                    .as_ref()
                    .is_some_and(|summary| summary.modeling == ModelingStatus::Opaque)
            })
            .count(),
        error_count: model
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == DiagnosticSeverity::Error)
            .count(),
        warning_count: model
            .diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == DiagnosticSeverity::Warning)
            .count(),
    }
}
