use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::model::{
    ArrayDimension, ExpressionKind, HazardKind, LiteralValue, ModelingStatus, OutcomeRole,
    PouSemanticModel, ProjectSemanticModel, SourceSpan, TargetKind,
};

pub const CODEGEN_MAP_SCHEMA: &str = "semantist.codegen-map/1.0.0";
pub const RUNTIME_ID_SCHEMA: &str = "semantist.runtime-ids/1.0.0";
pub const LLVM_METADATA_KIND: &str = "semantist.semantic";

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CrossLayerMappingStatus {
    Exact,
    Conservative,
    OneToMany,
    Eliminated,
    Unsupported,
    Unmapped,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeIdTable {
    pub schema_version: String,
    pub stg_schema_version: String,
    pub entries: Vec<RuntimeTarget>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeTarget {
    pub runtime_id: u32,
    pub stable_id: String,
    pub pou: String,
    pub node_id: String,
    pub semantic_edge_ids: Vec<String>,
    pub kind: TargetKind,
    pub role: Option<OutcomeRole>,
    pub hazard: Option<HazardKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub violation_predicate: Option<ViolationPredicate>,
    pub source: Option<SourceSpan>,
    pub modeling: ModelingStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ViolationPredicate {
    Division {
        signed: Option<bool>,
        bit_width: Option<u32>,
    },
    IntegerOverflow {
        operator: String,
        signed: Option<bool>,
        bit_width: Option<u32>,
    },
    ArrayBounds {
        dimensions: Vec<ArrayDimension>,
    },
    PointerNonNull,
    Narrowing {
        source_bit_width: Option<u32>,
        target_bit_width: Option<u32>,
        signed: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        source_signed: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        target_signed: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        source_constant: Option<String>,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CodegenMap {
    pub schema_version: String,
    pub stg_schema_version: String,
    pub rusty_revision: String,
    pub metadata_kind: String,
    pub anchors: Vec<CodegenAnchor>,
    pub diagnostics: Vec<CodegenMappingDiagnostic>,
    pub statistics: CodegenMappingStatistics,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CodegenAnchor {
    pub pou: String,
    pub lowered_ast_id: Option<usize>,
    pub kind: CodegenAnchorKind,
    pub source: Option<SourceSpan>,
    pub events: Vec<CodegenEvent>,
    pub status: CrossLayerMappingStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum CodegenAnchorKind {
    Conditional,
    CaseLabel,
    CaseDefault,
    LoopBack,
    ExplicitExit,
    ExplicitContinue,
    Return,
    FunctionEntry,
    FunctionExit,
    Hazard,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CodegenEvent {
    pub runtime_id: u32,
    pub stable_id: String,
    pub semantic_edge_ids: Vec<String>,
    pub kind: TargetKind,
    pub role: Option<OutcomeRole>,
    pub hazard: Option<HazardKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub violation_predicate: Option<ViolationPredicate>,
    pub successor_hint: Option<u32>,
    pub modeling: ModelingStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CodegenMappingDiagnostic {
    pub code: String,
    pub message: String,
    pub stable_id: String,
    pub pou: String,
    pub source: Option<SourceSpan>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CodegenMappingStatistics {
    pub target_count: usize,
    pub anchor_count: usize,
    pub exact_target_count: usize,
    pub one_to_many_target_count: usize,
    pub unmapped_target_count: usize,
}

pub fn build_runtime_ids(model: &ProjectSemanticModel) -> RuntimeIdTable {
    let mut entries = model
        .pous
        .iter()
        .flat_map(|pou| {
            pou.targets.iter().map(|target| {
                let hazard = pou
                    .graph
                    .nodes
                    .iter()
                    .find(|node| node.id == target.node_id)
                    .and_then(|node| node.hazard.as_ref())
                    .map(|hazard| hazard.kind.clone());
                let violation_predicate = violation_predicate(pou, target.node_id.as_str());
                RuntimeTarget {
                    runtime_id: 0,
                    stable_id: target.id.clone(),
                    pou: pou.pou.qualified_name.clone(),
                    node_id: target.node_id.clone(),
                    semantic_edge_ids: target.edge_ids.clone(),
                    kind: target.kind.clone(),
                    role: target.role.clone(),
                    hazard,
                    violation_predicate,
                    source: target.source.clone(),
                    modeling: target.modeling.clone(),
                }
            })
        })
        .collect::<Vec<_>>();
    entries.sort_by(|left, right| left.stable_id.cmp(&right.stable_id));
    for (runtime_id, entry) in entries.iter_mut().enumerate() {
        entry.runtime_id = runtime_id as u32;
    }
    RuntimeIdTable {
        schema_version: RUNTIME_ID_SCHEMA.to_string(),
        stg_schema_version: model.schema_version.clone(),
        entries,
    }
}

pub fn build_codegen_map(model: &ProjectSemanticModel, runtime_ids: &RuntimeIdTable) -> CodegenMap {
    let runtime_by_stable = runtime_ids
        .entries
        .iter()
        .map(|entry| (entry.stable_id.as_str(), entry))
        .collect::<BTreeMap<_, _>>();
    let mut anchors = Vec::new();
    let mut diagnostics = Vec::new();
    let mut exact_target_count = 0;
    let mut one_to_many_target_count = 0;
    let mut unmapped_target_count = 0;

    for pou in &model.pous {
        let node_mapping = pou
            .mappings
            .nodes
            .iter()
            .map(|mapping| (mapping.semantic_id.as_str(), mapping))
            .collect::<BTreeMap<_, _>>();
        let node_by_id = pou
            .graph
            .nodes
            .iter()
            .map(|node| (node.id.as_str(), node))
            .collect::<BTreeMap<_, _>>();

        for target in &pou.targets {
            let Some(runtime) = runtime_by_stable.get(target.id.as_str()) else {
                continue;
            };
            let event = CodegenEvent {
                runtime_id: runtime.runtime_id,
                stable_id: target.id.clone(),
                semantic_edge_ids: target.edge_ids.clone(),
                kind: target.kind.clone(),
                role: target.role.clone(),
                hazard: runtime.hazard.clone(),
                violation_predicate: runtime.violation_predicate.clone(),
                successor_hint: successor_hint(
                    target.role.as_ref(),
                    node_by_id.get(target.node_id.as_str()),
                ),
                modeling: target.modeling.clone(),
            };
            let kind = anchor_kind(target.kind.clone(), target.role.as_ref());
            let lowered_ids = node_mapping
                .get(target.node_id.as_str())
                .map(|mapping| mapping.lowered_ast_ids.clone())
                .unwrap_or_default();

            if matches!(
                target.role,
                Some(OutcomeRole::CycleEntry | OutcomeRole::CycleExit)
            ) {
                exact_target_count += 1;
                anchors.push(CodegenAnchor {
                    pou: pou.pou.qualified_name.clone(),
                    lowered_ast_id: None,
                    kind,
                    source: target.source.clone(),
                    events: vec![event],
                    status: CrossLayerMappingStatus::Exact,
                });
            } else if lowered_ids.is_empty() {
                unmapped_target_count += 1;
                diagnostics.push(CodegenMappingDiagnostic {
                    code: "STG-IR-E001".to_string(),
                    message: "semantic target has no Typed/Lowered AST anchor".to_string(),
                    stable_id: target.id.clone(),
                    pou: pou.pou.qualified_name.clone(),
                    source: target.source.clone(),
                });
            } else {
                if lowered_ids.len() == 1 {
                    exact_target_count += 1;
                } else {
                    one_to_many_target_count += 1;
                }
                let status = if lowered_ids.len() == 1 {
                    CrossLayerMappingStatus::Exact
                } else {
                    CrossLayerMappingStatus::OneToMany
                };
                for lowered_ast_id in lowered_ids {
                    anchors.push(CodegenAnchor {
                        pou: pou.pou.qualified_name.clone(),
                        lowered_ast_id: Some(lowered_ast_id),
                        kind: kind.clone(),
                        source: target.source.clone(),
                        events: vec![event.clone()],
                        status: status.clone(),
                    });
                }
            }
        }
    }

    anchors.sort_by(|left, right| {
        (
            &left.pou,
            left.lowered_ast_id,
            &left.kind,
            left.events.first().map(|event| event.runtime_id),
        )
            .cmp(&(
                &right.pou,
                right.lowered_ast_id,
                &right.kind,
                right.events.first().map(|event| event.runtime_id),
            ))
    });
    anchors = merge_anchors(anchors);
    diagnostics.sort_by(|left, right| left.stable_id.cmp(&right.stable_id));

    CodegenMap {
        schema_version: CODEGEN_MAP_SCHEMA.to_string(),
        stg_schema_version: model.schema_version.clone(),
        rusty_revision: model.metadata.rusty_revision.clone(),
        metadata_kind: LLVM_METADATA_KIND.to_string(),
        statistics: CodegenMappingStatistics {
            target_count: runtime_ids.entries.len(),
            anchor_count: anchors.len(),
            exact_target_count,
            one_to_many_target_count,
            unmapped_target_count,
        },
        anchors,
        diagnostics,
    }
}

fn violation_predicate(pou: &PouSemanticModel, node_id: &str) -> Option<ViolationPredicate> {
    let hazard = pou
        .graph
        .nodes
        .iter()
        .find(|node| node.id == node_id)?
        .hazard
        .as_ref()?;
    let expression = pou
        .environment
        .expressions
        .iter()
        .find(|expression| expression.id == hazard.expression_id)?;
    let expression_type = pou
        .environment
        .types
        .iter()
        .find(|ty| ty.name.eq_ignore_ascii_case(&expression.type_name));

    match hazard.kind {
        HazardKind::Division | HazardKind::Modulo => Some(ViolationPredicate::Division {
            signed: expression_type.and_then(|ty| ty.signed),
            bit_width: expression_type.and_then(|ty| ty.semantic_bit_width.or(ty.bit_width)),
        }),
        HazardKind::ArithmeticBoundary => {
            let ExpressionKind::Binary { operator } = &expression.kind else {
                return None;
            };
            Some(ViolationPredicate::IntegerOverflow {
                operator: operator.clone(),
                signed: expression_type.and_then(|ty| ty.signed),
                bit_width: expression_type.and_then(|ty| ty.semantic_bit_width.or(ty.bit_width)),
            })
        }
        HazardKind::ArrayAccess => {
            let base = expression.operands.first().and_then(|id| {
                pou.environment
                    .expressions
                    .iter()
                    .find(|candidate| candidate.id == *id)
            })?;
            let dimensions = pou
                .environment
                .types
                .iter()
                .find(|ty| ty.name.eq_ignore_ascii_case(&base.type_name))?
                .dimensions
                .clone();
            Some(ViolationPredicate::ArrayBounds { dimensions })
        }
        HazardKind::PointerAccess => Some(ViolationPredicate::PointerNonNull),
        HazardKind::DangerousConversion => {
            let target_type = expression.type_hint.as_deref().and_then(|name| {
                pou.environment
                    .types
                    .iter()
                    .find(|ty| ty.name.eq_ignore_ascii_case(name))
            });
            let source_signed = expression_type.and_then(|ty| ty.signed);
            let source_constant = match &expression.kind {
                ExpressionKind::Literal {
                    value: LiteralValue::Integer(value),
                } => Some(value.clone()),
                _ => None,
            };
            Some(ViolationPredicate::Narrowing {
                source_bit_width: expression_type
                    .and_then(|ty| ty.semantic_bit_width.or(ty.bit_width)),
                target_bit_width: target_type.and_then(|ty| ty.semantic_bit_width.or(ty.bit_width)),
                signed: source_signed,
                source_signed,
                target_signed: target_type.and_then(|ty| ty.signed),
                source_constant,
            })
        }
        HazardKind::ExternalOutput | HazardKind::UserProperty => None,
    }
}

fn merge_anchors(anchors: Vec<CodegenAnchor>) -> Vec<CodegenAnchor> {
    let mut merged: Vec<CodegenAnchor> = Vec::new();
    for mut anchor in anchors {
        if let Some(existing) = merged.last_mut().filter(|existing| {
            existing.pou == anchor.pou
                && existing.lowered_ast_id == anchor.lowered_ast_id
                && existing.kind == anchor.kind
        }) {
            existing.events.append(&mut anchor.events);
            existing
                .events
                .sort_by_key(|event| (event.successor_hint, event.runtime_id));
            existing
                .events
                .dedup_by_key(|event| (event.successor_hint, event.runtime_id));
            if existing.status != anchor.status {
                existing.status = CrossLayerMappingStatus::OneToMany;
            }
        } else {
            merged.push(anchor);
        }
    }
    merged
}

fn anchor_kind(kind: TargetKind, role: Option<&OutcomeRole>) -> CodegenAnchorKind {
    match role {
        Some(OutcomeRole::CaseLabel) => CodegenAnchorKind::CaseLabel,
        Some(OutcomeRole::CaseDefault) => CodegenAnchorKind::CaseDefault,
        Some(OutcomeRole::LoopBack) => CodegenAnchorKind::LoopBack,
        Some(OutcomeRole::ExplicitExit) => CodegenAnchorKind::ExplicitExit,
        Some(OutcomeRole::ExplicitContinue) => CodegenAnchorKind::ExplicitContinue,
        Some(OutcomeRole::Return) => CodegenAnchorKind::Return,
        Some(OutcomeRole::CycleEntry) => CodegenAnchorKind::FunctionEntry,
        Some(OutcomeRole::CycleExit) => CodegenAnchorKind::FunctionExit,
        _ if matches!(
            kind,
            TargetKind::HazardReach | TargetKind::HazardViolation | TargetKind::ExternalOutput
        ) =>
        {
            CodegenAnchorKind::Hazard
        }
        _ => CodegenAnchorKind::Conditional,
    }
}

fn successor_hint(
    role: Option<&OutcomeRole>,
    node: Option<&&crate::model::SemanticNode>,
) -> Option<u32> {
    match role {
        Some(OutcomeRole::True | OutcomeRole::LoopEnter) => Some(0),
        Some(OutcomeRole::False | OutcomeRole::Else) => Some(1),
        Some(OutcomeRole::LoopExit | OutcomeRole::LoopContinue) => node
            .and_then(|node| node.outcome.as_ref())
            .and_then(|outcome| match outcome.value {
                crate::model::OutcomeValue::Boolean(true) => Some(0),
                crate::model::OutcomeValue::Boolean(false) => Some(1),
                _ => None,
            }),
        _ => None,
    }
}
