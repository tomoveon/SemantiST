use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use anyhow::{bail, Result};
use plc::index::{Index, VariableType};
use plc::resolver::{AnnotationMap, AstAnnotations};
use plc::typesystem::{DataTypeInformation, StringEncoding};
use plc_ast::ast::{
    AstNode, AstStatement, BinaryExpression, CallStatement, CompilationUnit, Implementation,
    Operator, Pou, PouType, ReferenceAccess, ReferenceExpr,
};
use plc_ast::control_statements::{
    AstControlStatement, CaseStatement, ForLoopStatement, IfStatement, LoopStatement,
};
use plc_ast::literals::AstLiteral;
use plc_ast::visitor::{AstVisitor, Walker};
use plc_source::source_location::SourceLocation;

use crate::compiler::{normalize_path, CompilerSemanticProject, SourceConventions};
use crate::model::*;
use crate::stable_id::semantic_id;
use crate::{RUSTY_REVISION, RUSTY_VERSION};

pub fn extract_project(
    compilation: CompilerSemanticProject,
    pou_filters: &[String],
) -> Result<ProjectSemanticModel> {
    let lowered_facts = collect_lowered_facts(&compilation.lowered, &compilation.project_root);
    let mut semantic_sources = vec![
        "rusty_source_ast".to_string(),
        "rusty_index".to_string(),
        "rusty_type_annotations".to_string(),
        "rusty_typed_lowered_ast".to_string(),
    ];
    if compilation
        .source_conventions
        .has_configuration_annotations()
    {
        semantic_sources.push("var_input_constant_annotations".to_string());
    }
    let mut pous = Vec::new();

    for unit in &compilation.source.units {
        let source_unit = unit.get_unit();
        for implementation in &source_unit.implementations {
            if !is_supported_pou(&implementation.pou_type) {
                continue;
            }
            if !pou_filters.is_empty()
                && !pou_filters.iter().any(|filter| {
                    implementation.name.eq_ignore_ascii_case(filter)
                        || implementation.type_name.eq_ignore_ascii_case(filter)
                })
            {
                continue;
            }
            let declaration = find_declaration(source_unit, implementation);
            let mut builder = PouBuilder::new(
                &compilation.source.index,
                &compilation.source.annotations,
                &compilation.project_root,
                &lowered_facts,
                &compilation.source_conventions,
                declaration,
                implementation,
            )?;
            pous.push(builder.build()?);
        }
    }

    if pous.is_empty() {
        bail!(
            "no supported FUNCTION, FUNCTION_BLOCK, PROGRAM, METHOD, ACTION, or CLASS implementation found"
        );
    }
    pous.sort_by(|a, b| a.pou.qualified_name.cmp(&b.pou.qualified_name));

    let mut model = ProjectSemanticModel {
        schema_version: SCHEMA_VERSION.to_string(),
        metadata: BuildMetadata {
            generator: "semantist-stg".to_string(),
            generator_version: env!("CARGO_PKG_VERSION").to_string(),
            rusty_version: RUSTY_VERSION.to_string(),
            rusty_revision: RUSTY_REVISION.to_string(),
            project_root: normalize_path(&compilation.project_root),
            inputs: compilation.inputs,
            deterministic: true,
            semantic_sources,
        },
        pous,
        diagnostics: Vec::new(),
        statistics: ModelStatistics::default(),
    };
    recompute_statistics(&mut model);
    Ok(model)
}

fn is_supported_pou(kind: &PouType) -> bool {
    matches!(
        kind,
        PouType::Function
            | PouType::FunctionBlock
            | PouType::Program
            | PouType::Method { .. }
            | PouType::Action
            | PouType::Class
    )
}

fn find_declaration<'a>(
    unit: &'a CompilationUnit,
    implementation: &Implementation,
) -> Option<&'a Pou> {
    unit.pous.iter().find(|pou| {
        pou.name.eq_ignore_ascii_case(&implementation.type_name)
            || pou.name.eq_ignore_ascii_case(&implementation.name)
    })
}

#[derive(Default)]
struct LoweredSemanticFacts {
    spans: BTreeMap<SourceSpan, Vec<usize>>,
    for_exit_conditions: BTreeMap<(String, SourceSpan), Vec<usize>>,
    expression_nodes: BTreeMap<(String, String), Vec<(SourceSpan, usize)>>,
}

#[derive(Default)]
struct LoweredFactCollector {
    root: PathBuf,
    current_pou: Option<String>,
    facts: LoweredSemanticFacts,
}

impl AstVisitor for LoweredFactCollector {
    fn visit(&mut self, node: &AstNode) {
        if let Some(span) = source_span(&node.location, &self.root) {
            self.facts
                .spans
                .entry(span.clone())
                .or_default()
                .push(node.id);
            if let (Some(pou), Some(key)) =
                (self.current_pou.as_ref(), lowered_expression_key(node))
            {
                self.facts
                    .expression_nodes
                    .entry((pou.clone(), key))
                    .or_default()
                    .push((span.clone(), node.id));
            }
        }
        node.walk(self);
    }

    fn visit_implementation(&mut self, implementation: &Implementation) {
        let previous = self.current_pou.replace(implementation.name.clone());
        implementation.walk(self);
        self.current_pou = previous;
    }

    fn visit_control_statement(&mut self, statement: &AstControlStatement, _node: &AstNode) {
        if let (Some(pou), AstControlStatement::If(if_statement)) =
            (self.current_pou.as_ref(), statement)
        {
            for block in &if_statement.blocks {
                let direct_exit = block
                    .body
                    .iter()
                    .any(|node| matches!(node.get_stmt(), AstStatement::ExitStatement(())));
                let AstStatement::BinaryExpression(BinaryExpression {
                    operator: Operator::Greater | Operator::Less,
                    right,
                    ..
                }) = block.condition.get_stmt()
                else {
                    continue;
                };
                if !direct_exit {
                    continue;
                }
                if let Some(bound_span) = source_span(&right.location, &self.root) {
                    self.facts
                        .for_exit_conditions
                        .entry((pou.clone(), bound_span))
                        .or_default()
                        .push(block.condition.id);
                }
            }
        }
        statement.walk(self);
    }
}

fn collect_lowered_facts(
    project: &plc_driver::pipelines::AnnotatedProject,
    root: &Path,
) -> LoweredSemanticFacts {
    let mut collector = LoweredFactCollector {
        root: root.to_path_buf(),
        ..Default::default()
    };
    for unit in &project.units {
        collector.visit_compilation_unit(unit.get_unit());
    }
    for ids in collector.facts.spans.values_mut() {
        ids.sort_unstable();
        ids.dedup();
    }
    for ids in collector.facts.for_exit_conditions.values_mut() {
        ids.sort_unstable();
        ids.dedup();
    }
    for nodes in collector.facts.expression_nodes.values_mut() {
        nodes.sort_by(|left, right| {
            (left.0.start_offset, left.0.end_offset, left.1).cmp(&(
                right.0.start_offset,
                right.0.end_offset,
                right.1,
            ))
        });
        nodes.dedup();
    }
    collector.facts
}

impl LoweredSemanticFacts {
    fn expression_ids(&self, pou: &str, key: &str, source: &SourceSpan) -> Vec<usize> {
        let mut candidates = self
            .expression_nodes
            .get(&(pou.to_string(), key.to_string()))
            .into_iter()
            .flatten()
            .filter(|(candidate, _)| {
                candidate.file == source.file
                    && candidate.start_offset <= source.start_offset
                    && candidate.end_offset >= source.end_offset
            })
            .map(|(candidate, id)| {
                (
                    candidate.end_offset.saturating_sub(candidate.start_offset),
                    *id,
                )
            })
            .collect::<Vec<_>>();
        let Some(minimum_width) = candidates.iter().map(|(width, _)| *width).min() else {
            return Vec::new();
        };
        let mut ids = candidates
            .drain(..)
            .filter_map(|(width, id)| (width == minimum_width).then_some(id))
            .collect::<Vec<_>>();
        ids.sort_unstable();
        ids.dedup();
        ids
    }
}

struct PouBuilder<'a> {
    index: &'a Index,
    annotations: &'a AstAnnotations,
    root: &'a Path,
    lowered_facts: &'a LoweredSemanticFacts,
    source_conventions: &'a SourceConventions,
    declaration: Option<&'a Pou>,
    implementation: &'a Implementation,
    qualified_name: String,
    file_key: String,
    stateful: bool,
    environment: SemanticEnvironment,
    mappings: SemanticMappings,
    graph: SemanticGraph,
    targets: Vec<SemanticTarget>,
    diagnostics: Vec<ModelDiagnostic>,
    expression_by_ast: HashMap<usize, String>,
    expression_key_counts: HashMap<String, usize>,
    symbol_roles: BTreeMap<String, VariableRole>,
    node_ordinals: HashMap<String, usize>,
    edge_ordinal: usize,
    hazard_by_expression: BTreeSet<String>,
    return_target: Option<String>,
}

#[derive(Clone)]
struct FlowPoint {
    node: String,
    summary: TransferSummary,
    versions: BTreeMap<String, u32>,
}

#[derive(Clone)]
struct LoopContext {
    id: String,
    continue_target: String,
    exit_target: String,
}

#[derive(Clone)]
struct HazardCandidate {
    kind: HazardKind,
    expression_id: String,
    source: Option<SourceSpan>,
    detail: String,
    modeling: ModelingStatus,
}

impl<'a> PouBuilder<'a> {
    fn new(
        index: &'a Index,
        annotations: &'a AstAnnotations,
        root: &'a Path,
        lowered_facts: &'a LoweredSemanticFacts,
        source_conventions: &'a SourceConventions,
        declaration: Option<&'a Pou>,
        implementation: &'a Implementation,
    ) -> Result<Self> {
        let qualified_name = implementation.name.clone();
        let location = declaration
            .map(|pou| &pou.location)
            .unwrap_or(&implementation.location);
        let pou_span = source_span(location, root)
            .or_else(|| source_span(&implementation.location, root))
            .ok_or_else(|| anyhow::anyhow!("POU {} has no source location", qualified_name))?;
        let file_key = pou_span.file.clone();
        let stateful = implementation.pou_type.is_stateful();
        Ok(Self {
            index,
            annotations,
            root,
            lowered_facts,
            source_conventions,
            declaration,
            implementation,
            qualified_name,
            file_key,
            stateful,
            environment: SemanticEnvironment::default(),
            mappings: SemanticMappings::default(),
            graph: SemanticGraph::default(),
            targets: Vec::new(),
            diagnostics: Vec::new(),
            expression_by_ast: HashMap::new(),
            expression_key_counts: HashMap::new(),
            symbol_roles: BTreeMap::new(),
            node_ordinals: HashMap::new(),
            edge_ordinal: 0,
            hazard_by_expression: BTreeSet::new(),
            return_target: None,
        })
    }

    fn build(&mut self) -> Result<PouSemanticModel> {
        self.build_symbols_and_types();
        let pou_source = source_span(
            self.declaration
                .map(|pou| &pou.location)
                .unwrap_or(&self.implementation.location),
            self.root,
        )
        .expect("checked in constructor");
        let entry = self.add_node(NodeSpec::synthetic(NodeKind::Entry, "Entry"), 0, None);
        let exit = self.add_node(NodeSpec::synthetic(NodeKind::Exit, "Exit"), 0, None);
        self.graph.entry_id = entry.clone();
        self.graph.exit_id = exit.clone();

        let body_start;
        let body_end;
        if self.stateful {
            let cycle_entry = self.add_node(
                NodeSpec {
                    kind: NodeKind::CycleEntry,
                    label: "CycleEntry".to_string(),
                    source: Some(pou_source.clone()),
                    source_ast_ids: Vec::new(),
                    outcome: Some(Outcome {
                        predicate_id: None,
                        role: OutcomeRole::CycleEntry,
                        ordinal: 0,
                        value: OutcomeValue::Structural,
                    }),
                    hazard: None,
                    features: StaticFeatures::default(),
                },
                0,
                None,
            );
            let cycle_exit = self.add_node(
                NodeSpec {
                    kind: NodeKind::CycleExit,
                    label: "CycleExit".to_string(),
                    source: Some(pou_source.clone()),
                    source_ast_ids: Vec::new(),
                    outcome: Some(Outcome {
                        predicate_id: None,
                        role: OutcomeRole::CycleExit,
                        ordinal: 0,
                        value: OutcomeValue::Structural,
                    }),
                    hazard: None,
                    features: StaticFeatures::default(),
                },
                0,
                None,
            );
            self.graph.cycle_entry_id = Some(cycle_entry.clone());
            self.graph.cycle_exit_id = Some(cycle_exit.clone());
            self.add_control_edge(&entry, &cycle_entry, TransferSummary::default(), false);
            self.add_target(
                &cycle_entry,
                TargetKind::Cycle,
                Some(OutcomeRole::CycleEntry),
                ModelingStatus::Exact,
            );
            body_start = cycle_entry;
            body_end = cycle_exit.clone();
            self.add_target(
                &cycle_exit,
                TargetKind::Cycle,
                Some(OutcomeRole::CycleExit),
                ModelingStatus::Exact,
            );
            self.add_control_edge(&cycle_exit, &exit, TransferSummary::default(), false);

            let persistent = self
                .environment
                .persistent_state
                .as_ref()
                .map(|state| state.symbols.clone())
                .unwrap_or_default();
            self.add_temporal_edge(
                &cycle_exit,
                self.graph.cycle_entry_id.as_ref().unwrap().clone(),
                TemporalSummary {
                    modeling: ModelingStatus::Exact,
                    persistent_symbols: persistent,
                    rule: "next_cycle.initial(symbol) = current_cycle.final(symbol); external inputs may be refreshed at CycleEntry".to_string(),
                },
            );
        } else {
            body_start = entry;
            body_end = exit;
        }
        self.return_target = Some(body_end.clone());

        let initial = vec![FlowPoint {
            node: body_start,
            summary: TransferSummary::default(),
            versions: BTreeMap::new(),
        }];
        let endpoints = self.build_sequence(&self.implementation.statements, initial, 0, None)?;
        self.connect_points(endpoints, &body_end, false);

        self.ensure_expression_types();
        self.finalize_def_use();
        self.finalize_dependencies();
        self.finalize_target_edges();
        self.sort_model();

        let descriptor = PouDescriptor {
            name: self
                .declaration
                .map(|pou| pou.name.clone())
                .unwrap_or_else(|| self.implementation.type_name.clone()),
            qualified_name: self.qualified_name.clone(),
            kind: pou_kind(&self.implementation.pou_type),
            source: pou_source.clone(),
            stateful: self.stateful,
        };
        let model_id = semantic_id(
            "model",
            &self.file_key,
            &self.qualified_name,
            Some(&pou_source),
            "pou",
            0,
        );

        let mut model = PouSemanticModel {
            model_id,
            pou: descriptor,
            graph: std::mem::take(&mut self.graph),
            environment: std::mem::take(&mut self.environment),
            mappings: std::mem::take(&mut self.mappings),
            targets: std::mem::take(&mut self.targets),
            diagnostics: std::mem::take(&mut self.diagnostics),
            statistics: ModelStatistics::default(),
        };
        model.statistics = pou_statistics(&model);
        Ok(model)
    }

    fn build_symbols_and_types(&mut self) {
        let mut required_types = BTreeSet::new();
        let mut persistent_symbols = Vec::new();
        let mut cycle_inputs = Vec::new();
        let mut inout_symbols = Vec::new();
        let mut configuration_symbols = Vec::new();
        let mut initialization = BTreeMap::new();

        for variable in self.index.get_pou_members(&self.implementation.type_name) {
            let role = variable_role(variable.get_variable_type(), self.stateful);
            let qualified = variable.get_qualified_name().to_string();
            self.symbol_roles.insert(qualified.clone(), role.clone());
            required_types.insert(variable.get_type_name().to_string());
            let source = source_span(&variable.source_location, self.root).unwrap_or_else(|| {
                source_span(&self.implementation.location, self.root).expect("POU source")
            });
            let initial_value = self
                .index
                .get_initial_value(&variable.initial_value)
                .map(|node| self.expression(node));
            let configuration_source = if variable.get_variable_type() == VariableType::Input {
                if variable.is_constant() {
                    Some(ConfigurationSource::IecConstant)
                } else if self
                    .source_conventions
                    .is_configuration_input(&source.file, source.start_line)
                {
                    Some(ConfigurationSource::VarInputConstantAnnotation)
                } else {
                    None
                }
            } else {
                None
            };
            let is_persistent = self.stateful
                && matches!(
                    variable.get_variable_type(),
                    VariableType::Output | VariableType::Local | VariableType::Property
                );
            if is_persistent {
                persistent_symbols.push(qualified.clone());
                initialization.insert(qualified.clone(), initial_value.clone());
            }
            if self.stateful {
                match variable.get_variable_type() {
                    VariableType::Input if configuration_source.is_some() => {
                        configuration_symbols.push(qualified.clone());
                    }
                    VariableType::Input => cycle_inputs.push(qualified.clone()),
                    VariableType::InOut => inout_symbols.push(qualified.clone()),
                    _ => {}
                }
            }
            self.environment.symbols.push(Symbol {
                id: semantic_id(
                    "symbol",
                    &source.file,
                    &self.qualified_name,
                    Some(&source),
                    &qualified,
                    0,
                ),
                name: variable.get_name().to_string(),
                qualified_name: qualified,
                type_name: variable.get_type_name().to_string(),
                role,
                constant: variable.is_constant(),
                configuration_source,
                retain: variable.should_retain(self.index),
                by_reference: variable.get_declaration_type().is_by_ref(),
                source,
                initial_value,
                // RuSTy's semantic Index exposes field order and sizes, but not a
                // target ABI offset. Do not manufacture padding-free offsets.
                layout_offset_bits: None,
            });
        }

        let mut visited = BTreeSet::new();
        while let Some(type_name) = required_types.pop_first() {
            if !visited.insert(type_name.clone()) {
                continue;
            }
            if let Some(ty) = self.index.find_type(&type_name) {
                let model = type_model(self.index, ty, self.root);
                if let Some(element) = &model.element_type {
                    required_types.insert(element.clone());
                }
                if let Some(pointer) = &model.pointer_target {
                    required_types.insert(pointer.clone());
                }
                for field in &model.fields {
                    required_types.insert(field.type_name.clone());
                }
                self.environment.types.push(model);
            }
        }

        if self.stateful {
            persistent_symbols.sort();
            cycle_inputs.sort();
            inout_symbols.sort();
            configuration_symbols.sort();
            self.environment.persistent_state = Some(PersistentState {
                type_name: self.implementation.type_name.clone(),
                symbols: persistent_symbols,
                cycle_inputs,
                inout_symbols,
                configuration_symbols,
                initialization,
            });
        }
    }

    fn build_sequence(
        &mut self,
        statements: &[AstNode],
        mut points: Vec<FlowPoint>,
        depth: usize,
        loop_context: Option<LoopContext>,
    ) -> Result<Vec<FlowPoint>> {
        for statement in statements {
            match statement.get_stmt() {
                AstStatement::ControlStatement(control) => {
                    points = self.build_control(
                        statement,
                        control,
                        points,
                        depth,
                        loop_context.clone(),
                    )?;
                }
                AstStatement::ExitStatement(()) => {
                    let Some(context) = &loop_context else {
                        self.add_diagnostic(
                            "STG-E011",
                            DiagnosticSeverity::Error,
                            "EXIT appears outside a modeled loop",
                            None,
                            source_span(&statement.location, self.root),
                        );
                        points.clear();
                        continue;
                    };
                    let node = self.add_node(
                        NodeSpec {
                            kind: NodeKind::LoopControl,
                            label: "EXIT".to_string(),
                            source: source_span(&statement.location, self.root),
                            source_ast_ids: vec![statement.id],
                            outcome: Some(Outcome {
                                predicate_id: None,
                                role: OutcomeRole::ExplicitExit,
                                ordinal: 0,
                                value: OutcomeValue::Structural,
                            }),
                            hazard: None,
                            features: StaticFeatures {
                                nesting_depth: depth,
                                loop_id: Some(context.id.clone()),
                                ..Default::default()
                            },
                        },
                        0,
                        None,
                    );
                    self.connect_points(points, &node, false);
                    self.add_target(
                        &node,
                        TargetKind::ControlTransfer,
                        Some(OutcomeRole::ExplicitExit),
                        ModelingStatus::Exact,
                    );
                    self.add_control_edge(
                        &node,
                        &context.exit_target,
                        TransferSummary::default(),
                        false,
                    );
                    points = Vec::new();
                }
                AstStatement::ContinueStatement(()) => {
                    let Some(context) = &loop_context else {
                        self.add_diagnostic(
                            "STG-E012",
                            DiagnosticSeverity::Error,
                            "CONTINUE appears outside a modeled loop",
                            None,
                            source_span(&statement.location, self.root),
                        );
                        points.clear();
                        continue;
                    };
                    let node = self.add_node(
                        NodeSpec {
                            kind: NodeKind::LoopControl,
                            label: "CONTINUE".to_string(),
                            source: source_span(&statement.location, self.root),
                            source_ast_ids: vec![statement.id],
                            outcome: Some(Outcome {
                                predicate_id: None,
                                role: OutcomeRole::ExplicitContinue,
                                ordinal: 0,
                                value: OutcomeValue::Structural,
                            }),
                            hazard: None,
                            features: StaticFeatures {
                                nesting_depth: depth,
                                loop_id: Some(context.id.clone()),
                                ..Default::default()
                            },
                        },
                        0,
                        None,
                    );
                    self.connect_points(points, &node, false);
                    self.add_target(
                        &node,
                        TargetKind::ControlTransfer,
                        Some(OutcomeRole::ExplicitContinue),
                        ModelingStatus::Exact,
                    );
                    self.add_control_edge(
                        &node,
                        &context.continue_target,
                        TransferSummary::default(),
                        true,
                    );
                    points = Vec::new();
                }
                AstStatement::ReturnStatement(_) => {
                    let operation = TransferOperation::Return {
                        source: source_span(&statement.location, self.root).expect("return source"),
                    };
                    for point in &mut points {
                        apply_operation(&mut point.summary, &mut point.versions, operation.clone());
                    }
                    let return_node = self.add_node(
                        NodeSpec {
                            kind: NodeKind::LoopControl,
                            label: "RETURN".to_string(),
                            source: source_span(&statement.location, self.root),
                            source_ast_ids: vec![statement.id],
                            outcome: Some(Outcome {
                                predicate_id: None,
                                role: OutcomeRole::Return,
                                ordinal: 0,
                                value: OutcomeValue::Structural,
                            }),
                            hazard: None,
                            features: StaticFeatures {
                                nesting_depth: depth,
                                loop_id: loop_context.as_ref().map(|context| context.id.clone()),
                                ..Default::default()
                            },
                        },
                        0,
                        None,
                    );
                    self.connect_points(points, &return_node, false);
                    self.add_target(
                        &return_node,
                        TargetKind::ControlTransfer,
                        Some(OutcomeRole::Return),
                        ModelingStatus::Exact,
                    );
                    let return_target = self
                        .return_target
                        .clone()
                        .expect("return target initialized before body extraction");
                    self.add_control_edge(
                        &return_node,
                        &return_target,
                        TransferSummary::default(),
                        false,
                    );
                    points = Vec::new();
                }
                _ => {
                    let (operation, hazards) = self.summarize_statement(statement, &mut points);
                    for hazard in hazards {
                        points = self.insert_hazard(points, hazard, depth, loop_context.as_ref());
                    }
                    if let Some(operation) = operation {
                        for point in &mut points {
                            apply_operation(
                                &mut point.summary,
                                &mut point.versions,
                                operation.clone(),
                            );
                        }
                    }
                }
            }
        }
        Ok(points)
    }

    fn build_control(
        &mut self,
        node: &AstNode,
        control: &AstControlStatement,
        points: Vec<FlowPoint>,
        depth: usize,
        loop_context: Option<LoopContext>,
    ) -> Result<Vec<FlowPoint>> {
        match control {
            AstControlStatement::If(statement) => {
                self.build_if(node, statement, points, depth, loop_context)
            }
            AstControlStatement::Case(statement) => {
                self.build_case(node, statement, points, depth, loop_context)
            }
            AstControlStatement::WhileLoop(statement) => {
                self.build_while(node, statement, points, depth)
            }
            AstControlStatement::RepeatLoop(statement) => {
                self.build_repeat(node, statement, points, depth)
            }
            AstControlStatement::ForLoop(statement) => {
                self.build_for(node, statement, points, depth)
            }
        }
    }

    fn build_if(
        &mut self,
        node: &AstNode,
        statement: &IfStatement,
        points: Vec<FlowPoint>,
        depth: usize,
        loop_context: Option<LoopContext>,
    ) -> Result<Vec<FlowPoint>> {
        let mut false_points = points;
        let mut completed = Vec::new();
        for (index, block) in statement.blocks.iter().enumerate() {
            let kind = if index == 0 {
                PredicateKind::If
            } else {
                PredicateKind::Elsif
            };
            let predicate = self.add_predicate(node, &block.condition, kind, depth, index, None);
            self.connect_points(false_points, &predicate, false);

            let true_outcome = self.add_outcome(
                &predicate,
                &block.condition,
                OutcomeRole::True,
                OutcomeValue::Boolean(true),
                0,
                depth,
                None,
            );
            let false_outcome = self.add_outcome(
                &predicate,
                &block.condition,
                OutcomeRole::False,
                OutcomeValue::Boolean(false),
                1,
                depth,
                None,
            );
            let guard = self.expression(&block.condition);
            let negated = self.synthetic_unary_expression(
                "NOT",
                &guard,
                "BOOL",
                block.condition.location.clone(),
            );
            self.add_evaluation_edge(&predicate, &true_outcome, &guard);
            self.add_evaluation_edge(&predicate, &false_outcome, &negated);

            let true_endpoints = self.build_sequence(
                &block.body,
                vec![FlowPoint::new(true_outcome)],
                depth + 1,
                loop_context.clone(),
            )?;
            completed.extend(true_endpoints);
            if index + 1 == statement.blocks.len() && !statement.else_block.is_empty() {
                let else_entry = self.add_structural_else(&block.condition, depth, &predicate);
                self.add_control_edge(
                    &false_outcome,
                    &else_entry,
                    TransferSummary::default(),
                    false,
                );
                false_points = vec![FlowPoint::new(else_entry)];
            } else {
                false_points = vec![FlowPoint::new(false_outcome)];
            }
        }

        let else_endpoints =
            self.build_sequence(&statement.else_block, false_points, depth + 1, loop_context)?;
        completed.extend(else_endpoints);
        Ok(completed)
    }

    fn build_case(
        &mut self,
        node: &AstNode,
        statement: &CaseStatement,
        points: Vec<FlowPoint>,
        depth: usize,
        loop_context: Option<LoopContext>,
    ) -> Result<Vec<FlowPoint>> {
        let predicate = self.add_predicate(
            node,
            &statement.selector,
            PredicateKind::Case,
            depth,
            0,
            None,
        );
        self.connect_points(points, &predicate, false);
        let selector = self.expression(&statement.selector);
        let mut endpoints = Vec::new();
        let mut label_guards = Vec::new();

        for (block_index, block) in statement.case_blocks.iter().enumerate() {
            let labels = case_labels(&block.condition);
            for (label_index, label) in labels.iter().enumerate() {
                let label_expr = self.expression(label);
                let guard = self.case_guard_expression(&selector, &label_expr, label);
                label_guards.push(guard.clone());
                let ordinal = block_index * 1024 + label_index;
                let outcome = self.add_outcome(
                    &predicate,
                    label,
                    OutcomeRole::CaseLabel,
                    OutcomeValue::Expression(label_expr),
                    ordinal,
                    depth,
                    None,
                );
                self.add_evaluation_edge(&predicate, &outcome, &guard);
                endpoints.extend(self.build_sequence(
                    &block.body,
                    vec![FlowPoint::new(outcome)],
                    depth + 1,
                    loop_context.clone(),
                )?);
            }
        }

        let default = self.add_outcome(
            &predicate,
            &statement.selector,
            OutcomeRole::CaseDefault,
            OutcomeValue::Default,
            statement.case_blocks.len() * 1024,
            depth,
            None,
        );
        let default_guard =
            self.synthetic_nor_expression(&label_guards, statement.selector.location.clone());
        self.add_evaluation_edge(&predicate, &default, &default_guard);
        endpoints.extend(self.build_sequence(
            &statement.else_block,
            vec![FlowPoint::new(default)],
            depth + 1,
            loop_context,
        )?);
        Ok(endpoints)
    }

    fn build_while(
        &mut self,
        node: &AstNode,
        statement: &LoopStatement,
        points: Vec<FlowPoint>,
        depth: usize,
    ) -> Result<Vec<FlowPoint>> {
        let predicate = self.add_predicate(
            node,
            &statement.condition,
            PredicateKind::While,
            depth,
            0,
            None,
        );
        self.connect_points(points, &predicate, false);
        let enter = self.add_outcome(
            &predicate,
            &statement.condition,
            OutcomeRole::LoopEnter,
            OutcomeValue::Boolean(true),
            0,
            depth,
            Some(predicate.clone()),
        );
        let exit = self.add_outcome(
            &predicate,
            &statement.condition,
            OutcomeRole::LoopExit,
            OutcomeValue::Boolean(false),
            1,
            depth,
            Some(predicate.clone()),
        );
        let guard = self.expression(&statement.condition);
        let negated = self.synthetic_unary_expression(
            "NOT",
            &guard,
            "BOOL",
            statement.condition.location.clone(),
        );
        self.add_evaluation_edge(&predicate, &enter, &guard);
        self.add_evaluation_edge(&predicate, &exit, &negated);
        let context = LoopContext {
            id: predicate.clone(),
            continue_target: predicate.clone(),
            exit_target: exit.clone(),
        };
        let body = self.build_sequence(
            &statement.body,
            vec![FlowPoint::new(enter)],
            depth + 1,
            Some(context),
        )?;
        let latch = self.add_loop_back(node, depth, &predicate, "WHILE back");
        self.connect_points(body, &latch, false);
        self.add_control_edge(&latch, &predicate, TransferSummary::default(), true);
        Ok(vec![FlowPoint::new(exit)])
    }

    fn build_repeat(
        &mut self,
        node: &AstNode,
        statement: &LoopStatement,
        points: Vec<FlowPoint>,
        depth: usize,
    ) -> Result<Vec<FlowPoint>> {
        let predicate = self.add_predicate(
            node,
            &statement.condition,
            PredicateKind::Repeat,
            depth,
            0,
            None,
        );
        let enter = self.add_structural_loop_enter(node, depth, &predicate);
        self.connect_points(points, &enter, false);
        let exit = self.add_outcome(
            &predicate,
            &statement.condition,
            OutcomeRole::LoopExit,
            OutcomeValue::Boolean(true),
            0,
            depth,
            Some(predicate.clone()),
        );
        let repeat = self.add_outcome(
            &predicate,
            &statement.condition,
            OutcomeRole::LoopContinue,
            OutcomeValue::Boolean(false),
            1,
            depth,
            Some(predicate.clone()),
        );
        let guard = self.expression(&statement.condition);
        let negated = self.synthetic_unary_expression(
            "NOT",
            &guard,
            "BOOL",
            statement.condition.location.clone(),
        );
        self.add_evaluation_edge(&predicate, &exit, &guard);
        self.add_evaluation_edge(&predicate, &repeat, &negated);
        let latch = self.add_loop_back(node, depth, &predicate, "REPEAT back");
        self.add_control_edge(&repeat, &latch, TransferSummary::default(), false);
        self.add_control_edge(&latch, &enter, TransferSummary::default(), true);
        let context = LoopContext {
            id: predicate.clone(),
            continue_target: predicate.clone(),
            exit_target: exit.clone(),
        };
        let body = self.build_sequence(
            &statement.body,
            vec![FlowPoint::new(enter)],
            depth + 1,
            Some(context),
        )?;
        self.connect_points(body, &predicate, false);
        Ok(vec![FlowPoint::new(exit)])
    }

    fn build_for(
        &mut self,
        node: &AstNode,
        statement: &ForLoopStatement,
        mut points: Vec<FlowPoint>,
        depth: usize,
    ) -> Result<Vec<FlowPoint>> {
        let counter = self.expression(&statement.counter);
        let start = self.expression(&statement.start);
        let start_reads = self.expression_reads(&start);
        let start_modeling = self.expression_modeling_by_id(&start);
        let init_source = source_span(&node.location, self.root).expect("for source");
        let init = TransferOperation::Assign {
            target: counter.clone(),
            target_symbol: self.expression_variable_name(&counter),
            value: start,
            reads: start_reads,
            modeling: start_modeling,
            version_before: 0,
            version_after: 0,
            conversion: self.conversion_for(&statement.counter, &statement.start),
            source: init_source,
        };
        for point in &mut points {
            apply_operation(&mut point.summary, &mut point.versions, init.clone());
        }

        let predicate =
            self.add_predicate(node, &statement.end, PredicateKind::For, depth, 0, None);
        self.connect_points(points, &predicate, false);
        let enter = self.add_outcome(
            &predicate,
            &statement.end,
            OutcomeRole::LoopEnter,
            OutcomeValue::Boolean(true),
            0,
            depth,
            Some(predicate.clone()),
        );
        let exit = self.add_outcome(
            &predicate,
            &statement.end,
            OutcomeRole::LoopExit,
            OutcomeValue::Boolean(false),
            1,
            depth,
            Some(predicate.clone()),
        );
        if let Some(bound_span) = source_span(&statement.end.location, self.root) {
            let lowered_ids = self
                .lowered_facts
                .for_exit_conditions
                .get(&(self.qualified_name.clone(), bound_span))
                .cloned()
                .unwrap_or_default();
            self.extend_node_lowered_ids(&enter, &lowered_ids);
            self.extend_node_lowered_ids(&exit, &lowered_ids);
        }
        let condition = self.for_condition_expression(statement);
        let negated = self.synthetic_unary_expression(
            "NOT",
            &condition,
            "BOOL",
            statement.end.location.clone(),
        );
        self.add_evaluation_edge(&predicate, &enter, &condition);
        self.add_evaluation_edge(&predicate, &exit, &negated);

        let latch = self.add_node(
            NodeSpec {
                kind: NodeKind::LoopLatch,
                label: "FOR latch".to_string(),
                source: source_span(&node.location, self.root),
                source_ast_ids: vec![node.id],
                outcome: Some(Outcome {
                    predicate_id: None,
                    role: OutcomeRole::LoopBack,
                    ordinal: 0,
                    value: OutcomeValue::Structural,
                }),
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    loop_id: Some(predicate.clone()),
                    ..Default::default()
                },
            },
            0,
            Some(predicate.clone()),
        );
        self.add_target(
            &latch,
            TargetKind::LoopOutcome,
            Some(OutcomeRole::LoopBack),
            ModelingStatus::Exact,
        );
        let context = LoopContext {
            id: predicate.clone(),
            continue_target: latch.clone(),
            exit_target: exit.clone(),
        };
        let body = self.build_sequence(
            &statement.body,
            vec![FlowPoint::new(enter)],
            depth + 1,
            Some(context),
        )?;
        self.connect_points(body, &latch, false);

        let step = statement
            .by_step
            .as_deref()
            .map(|node| self.expression(node))
            .unwrap_or_else(|| self.synthetic_integer_expression(1, &statement.end.location));
        let increment = self.synthetic_binary_expression(
            "+",
            &counter,
            &step,
            self.expression_type(&statement.counter),
            statement.counter.location.clone(),
        );
        let target_name = self.expression_variable_name(&counter);
        let increment_reads = self.expression_reads(&increment);
        let increment_modeling = self.expression_modeling_by_id(&increment);
        let mut summary = TransferSummary::default();
        let mut versions = BTreeMap::new();
        let operation = TransferOperation::Assign {
            target: counter,
            target_symbol: target_name.clone(),
            value: increment,
            reads: increment_reads,
            modeling: increment_modeling,
            version_before: 0,
            version_after: 0,
            conversion: None,
            source: source_span(&node.location, self.root).expect("for source"),
        };
        apply_operation(&mut summary, &mut versions, operation);
        if target_name.is_none() {
            summary.modeling = ModelingStatus::Conservative;
        }
        self.add_control_edge(&latch, &predicate, summary, true);
        Ok(vec![FlowPoint::new(exit)])
    }

    fn add_predicate(
        &mut self,
        control: &AstNode,
        condition: &AstNode,
        kind: PredicateKind,
        depth: usize,
        ordinal: usize,
        loop_id: Option<String>,
    ) -> String {
        let expression = self.expression(condition);
        let reads = self.expression_reads(&expression);
        let input_dependencies = reads
            .iter()
            .filter(|name| self.symbol_roles.get(*name) == Some(&VariableRole::Input))
            .count();
        let state_dependencies = reads
            .iter()
            .filter(|name| self.symbol_roles.get(*name) == Some(&VariableRole::PersistentState))
            .count();
        self.add_node(
            NodeSpec {
                kind: NodeKind::Predicate(kind.clone()),
                label: format!("{kind:?} predicate"),
                source: source_span(&condition.location, self.root),
                source_ast_ids: vec![control.id, condition.id],
                outcome: None,
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    condition_complexity: self.expression_complexity(&expression),
                    input_dependencies,
                    state_dependencies,
                    loop_id,
                    call_complexity: self.expression_call_count(&expression),
                },
            },
            ordinal,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn add_outcome(
        &mut self,
        predicate: &str,
        source_node: &AstNode,
        role: OutcomeRole,
        value: OutcomeValue,
        ordinal: usize,
        depth: usize,
        loop_id: Option<String>,
    ) -> String {
        let node_id = self.add_node(
            NodeSpec {
                kind: NodeKind::Outcome,
                label: format!("{role:?}"),
                source: source_span(&source_node.location, self.root),
                source_ast_ids: vec![source_node.id],
                outcome: Some(Outcome {
                    predicate_id: Some(predicate.to_string()),
                    role: role.clone(),
                    ordinal,
                    value,
                }),
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    loop_id,
                    ..Default::default()
                },
            },
            ordinal,
            Some(predicate.to_string()),
        );
        let target_kind = match role {
            OutcomeRole::CaseLabel | OutcomeRole::CaseDefault => TargetKind::CaseOutcome,
            OutcomeRole::LoopEnter
            | OutcomeRole::LoopContinue
            | OutcomeRole::LoopExit
            | OutcomeRole::LoopBack => TargetKind::LoopOutcome,
            OutcomeRole::ExplicitExit | OutcomeRole::ExplicitContinue | OutcomeRole::Return => {
                TargetKind::ControlTransfer
            }
            OutcomeRole::CycleEntry | OutcomeRole::CycleExit => TargetKind::Cycle,
            _ => TargetKind::BranchOutcome,
        };
        self.add_target(&node_id, target_kind, Some(role), ModelingStatus::Exact);
        node_id
    }

    fn add_structural_loop_enter(
        &mut self,
        source_node: &AstNode,
        depth: usize,
        loop_id: &str,
    ) -> String {
        let node_id = self.add_node(
            NodeSpec {
                kind: NodeKind::Outcome,
                label: "LoopEnter".to_string(),
                source: source_span(&source_node.location, self.root),
                source_ast_ids: vec![source_node.id],
                outcome: Some(Outcome {
                    predicate_id: None,
                    role: OutcomeRole::LoopEnter,
                    ordinal: 0,
                    value: OutcomeValue::Structural,
                }),
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    loop_id: Some(loop_id.to_string()),
                    ..Default::default()
                },
            },
            0,
            Some(loop_id.to_string()),
        );
        self.add_target(
            &node_id,
            TargetKind::LoopOutcome,
            Some(OutcomeRole::LoopEnter),
            ModelingStatus::Exact,
        );
        node_id
    }

    fn add_structural_else(
        &mut self,
        source_node: &AstNode,
        depth: usize,
        predicate: &str,
    ) -> String {
        let node_id = self.add_node(
            NodeSpec {
                kind: NodeKind::Outcome,
                label: "Else".to_string(),
                source: source_span(&source_node.location, self.root),
                source_ast_ids: vec![source_node.id],
                outcome: Some(Outcome {
                    predicate_id: None,
                    role: OutcomeRole::Else,
                    ordinal: 2,
                    value: OutcomeValue::Structural,
                }),
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    ..Default::default()
                },
            },
            2,
            Some(predicate.to_string()),
        );
        self.add_target(
            &node_id,
            TargetKind::BranchOutcome,
            Some(OutcomeRole::Else),
            ModelingStatus::Exact,
        );
        node_id
    }

    fn add_loop_back(
        &mut self,
        source_node: &AstNode,
        depth: usize,
        loop_id: &str,
        label: &str,
    ) -> String {
        let node_id = self.add_node(
            NodeSpec {
                kind: NodeKind::LoopLatch,
                label: label.to_string(),
                source: source_span(&source_node.location, self.root),
                source_ast_ids: vec![source_node.id],
                outcome: Some(Outcome {
                    predicate_id: None,
                    role: OutcomeRole::LoopBack,
                    ordinal: 0,
                    value: OutcomeValue::Structural,
                }),
                hazard: None,
                features: StaticFeatures {
                    nesting_depth: depth,
                    loop_id: Some(loop_id.to_string()),
                    ..Default::default()
                },
            },
            0,
            Some(loop_id.to_string()),
        );
        self.add_target(
            &node_id,
            TargetKind::LoopOutcome,
            Some(OutcomeRole::LoopBack),
            ModelingStatus::Exact,
        );
        node_id
    }

    fn summarize_statement(
        &mut self,
        statement: &AstNode,
        _points: &mut [FlowPoint],
    ) -> (Option<TransferOperation>, Vec<HazardCandidate>) {
        let hazard_start = self.environment.expressions.len();
        let mut external_output = None;
        let mut dangerous_conversion = None;
        let operation = match statement.get_stmt() {
            AstStatement::Assignment(assignment)
            | AstStatement::OutputAssignment(assignment)
            | AstStatement::RefAssignment(assignment) => {
                let target = self.expression(&assignment.left);
                let value = self.expression(&assignment.right);
                let source = source_span(&statement.location, self.root)
                    .or_else(|| source_span(&assignment.left.location, self.root))
                    .expect("assignment source");
                if self.is_external_output_target(&target) {
                    external_output = Some(HazardCandidate {
                        kind: HazardKind::ExternalOutput,
                        expression_id: target.clone(),
                        source: Some(source.clone()),
                        detail: "assignment updates an externally visible output".to_string(),
                        modeling: ModelingStatus::Exact,
                    });
                }
                let conversion = self.conversion_for(&assignment.left, &assignment.right);
                if conversion
                    .as_ref()
                    .is_some_and(|conversion| conversion.potentially_narrowing)
                {
                    let modeling = self
                        .environment
                        .expressions
                        .iter()
                        .find(|expression| expression.id == value)
                        .is_some_and(|expression| {
                            matches!(
                                expression.kind,
                                ExpressionKind::Literal {
                                    value: LiteralValue::Integer(_)
                                }
                            )
                        })
                        .then_some(ModelingStatus::Exact)
                        .unwrap_or(ModelingStatus::Conservative);
                    dangerous_conversion = Some(HazardCandidate {
                        kind: HazardKind::DangerousConversion,
                        expression_id: value.clone(),
                        source: Some(source.clone()),
                        detail: "assignment performs a potentially narrowing IEC conversion"
                            .to_string(),
                        modeling,
                    });
                }
                Some(TransferOperation::Assign {
                    target: target.clone(),
                    target_symbol: self.expression_variable_name(&target),
                    reads: self.expression_reads(&value),
                    modeling: self.expression_modeling_by_id(&value),
                    value,
                    version_before: 0,
                    version_after: 0,
                    conversion,
                    source,
                })
            }
            AstStatement::CallStatement(call) => {
                let call_expression = self.expression(statement);
                let callee = self
                    .annotations
                    .get_call_name(&call.operator)
                    .map(str::to_string);
                let arguments = self
                    .environment
                    .expressions
                    .iter()
                    .find(|expr| expr.id == call_expression)
                    .map(|expr| expr.operands.clone())
                    .unwrap_or_default();
                let effects = self.call_effects(callee.as_deref(), &arguments);
                self.register_call_summary(callee.as_deref(), &effects);
                Some(TransferOperation::Call {
                    callee,
                    arguments,
                    effects,
                    source: source_span(&statement.location, self.root).expect("call source"),
                })
            }
            AstStatement::AllocationStatement(allocation) => Some(TransferOperation::Allocation {
                name: allocation.name.clone(),
                type_name: allocation.reference_type.clone(),
                source: source_span(&statement.location, self.root).unwrap_or_else(|| {
                    source_span(&self.implementation.location, self.root).unwrap()
                }),
            }),
            AstStatement::EmptyStatement(_) | AstStatement::LabelStatement(_) => None,
            other => Some(TransferOperation::Opaque {
                ast_kind: ast_kind(other).to_string(),
                reason: "statement is preserved as an explicit opaque compiler AST operation"
                    .to_string(),
                source: source_span(&statement.location, self.root),
            }),
        };
        let mut hazards = self.hazards_since(hazard_start);
        if let Some(candidate) = external_output {
            let key = format!("{:?}:{}", candidate.kind, candidate.expression_id);
            if self.hazard_by_expression.insert(key) {
                hazards.push(candidate);
            }
        }
        if let Some(candidate) = dangerous_conversion {
            let key = format!("{:?}:{}", candidate.kind, candidate.expression_id);
            if self.hazard_by_expression.insert(key) {
                hazards.push(candidate);
            }
        }
        (operation, hazards)
    }

    fn hazards_since(&mut self, start: usize) -> Vec<HazardCandidate> {
        let expressions = self.environment.expressions[start..].to_vec();
        let mut hazards = Vec::new();
        for expression in expressions {
            let candidates = hazards_for_expression(&expression, &self.environment.types);
            for candidate in candidates {
                if self
                    .hazard_by_expression
                    .insert(format!("{:?}:{}", candidate.kind, candidate.expression_id))
                {
                    hazards.push(candidate);
                }
            }
        }
        hazards
    }

    fn insert_hazard(
        &mut self,
        points: Vec<FlowPoint>,
        hazard: HazardCandidate,
        depth: usize,
        loop_context: Option<&LoopContext>,
    ) -> Vec<FlowPoint> {
        let node = self.add_node(
            NodeSpec {
                kind: NodeKind::Hazard,
                label: format!("{:?}", hazard.kind),
                source: hazard.source.clone(),
                source_ast_ids: Vec::new(),
                outcome: None,
                hazard: Some(Hazard {
                    kind: hazard.kind.clone(),
                    expression_id: hazard.expression_id.clone(),
                    modeling: hazard.modeling.clone(),
                    detail: hazard.detail,
                }),
                features: StaticFeatures {
                    nesting_depth: depth,
                    loop_id: loop_context.map(|context| context.id.clone()),
                    ..Default::default()
                },
            },
            0,
            None,
        );
        self.connect_points(points, &node, false);
        if hazard.kind == HazardKind::ExternalOutput {
            self.add_target(&node, TargetKind::ExternalOutput, None, hazard.modeling);
        } else {
            self.add_target(
                &node,
                TargetKind::HazardReach,
                None,
                hazard.modeling.clone(),
            );
            self.add_target(&node, TargetKind::HazardViolation, None, hazard.modeling);
        }
        vec![FlowPoint::new(node)]
    }

    fn expression(&mut self, node: &AstNode) -> String {
        if let Some(id) = self.expression_by_ast.get(&node.id) {
            return id.clone();
        }
        let span = source_span(&node.location, self.root);
        let type_name = self
            .annotations
            .get_type(node, self.index)
            .map(|ty| ty.get_name().to_string())
            .unwrap_or_else(|| "VOID".to_string());
        let type_hint = self
            .annotations
            .get_type_hint(node, self.index)
            .map(|ty| ty.get_name().to_string());

        let (kind, operands, reads, modeling) = match node.get_stmt() {
            AstStatement::Literal(literal) => (
                ExpressionKind::Literal {
                    value: literal_value(literal),
                },
                Vec::new(),
                BTreeSet::new(),
                ModelingStatus::Exact,
            ),
            AstStatement::Identifier(_) | AstStatement::This | AstStatement::Super(_) => {
                let qualified = self
                    .annotations
                    .get_qualified_name(node)
                    .map(str::to_string)
                    .or_else(|| node.get_flat_reference_name().map(str::to_string))
                    .unwrap_or_else(|| "<unresolved>".to_string());
                let mut reads = BTreeSet::new();
                if qualified != "<unresolved>" {
                    reads.insert(qualified.clone());
                }
                (
                    ExpressionKind::Variable {
                        qualified_name: qualified,
                    },
                    Vec::new(),
                    reads,
                    if self.annotations.get_qualified_name(node).is_some() {
                        ModelingStatus::Exact
                    } else {
                        ModelingStatus::Conservative
                    },
                )
            }
            AstStatement::BinaryExpression(BinaryExpression {
                operator,
                left,
                right,
            }) => {
                let left_id = self.expression(left);
                let right_id = self.expression(right);
                let modeling = self.max_expression_modeling(&[left_id.clone(), right_id.clone()]);
                (
                    ExpressionKind::Binary {
                        operator: operator.to_string(),
                    },
                    vec![left_id.clone(), right_id.clone()],
                    self.union_reads(&[left_id, right_id]),
                    modeling,
                )
            }
            AstStatement::UnaryExpression(unary) => {
                let value = self.expression(&unary.value);
                let modeling = self.expression_modeling_by_id(&value);
                (
                    ExpressionKind::Unary {
                        operator: unary.operator.to_string(),
                    },
                    vec![value.clone()],
                    self.expression_reads(&value),
                    modeling,
                )
            }
            AstStatement::ParenExpression(inner) | AstStatement::CaseCondition(inner) => {
                let inner = self.expression(inner);
                let modeling = self.expression_modeling_by_id(&inner);
                (
                    if matches!(node.get_stmt(), AstStatement::CaseCondition(_)) {
                        ExpressionKind::CaseCondition
                    } else {
                        ExpressionKind::List
                    },
                    vec![inner.clone()],
                    self.expression_reads(&inner),
                    modeling,
                )
            }
            AstStatement::ExpressionList(items) => {
                let operands = items
                    .iter()
                    .map(|item| self.expression(item))
                    .collect::<Vec<_>>();
                (
                    ExpressionKind::List,
                    operands.clone(),
                    self.union_reads(&operands),
                    self.max_expression_modeling(&operands),
                )
            }
            AstStatement::RangeStatement(range) => {
                let start = self.expression(&range.start);
                let end = self.expression(&range.end);
                let reads = self.union_reads(&[start.clone(), end.clone()]);
                let modeling = self.max_expression_modeling(&[start.clone(), end.clone()]);
                (
                    ExpressionKind::Range,
                    vec![start.clone(), end.clone()],
                    reads,
                    modeling,
                )
            }
            AstStatement::ReferenceExpr(reference) => self.reference_expression(node, reference),
            AstStatement::DirectAccess(access) => {
                let index = self.expression(&access.index);
                (
                    ExpressionKind::DirectAccess {
                        width_bits: access.access.get_bit_width(),
                    },
                    vec![index.clone()],
                    self.expression_reads(&index),
                    self.expression_modeling_by_id(&index),
                )
            }
            AstStatement::HardwareAccess(access) => {
                let operands = access
                    .address
                    .iter()
                    .map(|part| self.expression(part))
                    .collect::<Vec<_>>();
                (
                    ExpressionKind::HardwareAccess,
                    operands.clone(),
                    self.union_reads(&operands),
                    self.max_expression_modeling(&operands),
                )
            }
            AstStatement::CallStatement(call) => self.call_expression(call),
            AstStatement::Assignment(assignment)
            | AstStatement::OutputAssignment(assignment)
            | AstStatement::RefAssignment(assignment) => {
                let left = self.expression(&assignment.left);
                let right = self.expression(&assignment.right);
                let reads = self.union_reads(&[left.clone(), right.clone()]);
                let modeling = self.max_expression_modeling(&[left.clone(), right.clone()]);
                (
                    ExpressionKind::List,
                    vec![left.clone(), right.clone()],
                    reads,
                    modeling,
                )
            }
            other => (
                ExpressionKind::Opaque {
                    ast_kind: ast_kind(other).to_string(),
                },
                Vec::new(),
                BTreeSet::new(),
                ModelingStatus::Opaque,
            ),
        };

        let key = format!(
            "{}|{}|{}",
            span.as_ref()
                .map(|span| format!("{}:{}:{}", span.file, span.start_offset, span.end_offset))
                .unwrap_or_else(|| "synthetic".to_string()),
            expression_kind_name(&kind),
            type_name
        );
        let ordinal = self.expression_key_counts.entry(key).or_insert(0);
        let id = semantic_id(
            "expr",
            span.as_ref()
                .map(|s| s.file.as_str())
                .unwrap_or(&self.file_key),
            &self.qualified_name,
            span.as_ref(),
            expression_kind_name(&kind),
            *ordinal,
        );
        *ordinal += 1;
        let lowered_ast_ids = span
            .as_ref()
            .map(|span| {
                self.lowered_facts
                    .spans
                    .get(span)
                    .cloned()
                    .filter(|ids| !ids.is_empty())
                    .unwrap_or_else(|| {
                        self.lowered_facts.expression_ids(
                            &self.qualified_name,
                            &semantic_expression_key(&kind),
                            span,
                        )
                    })
            })
            .unwrap_or_default();
        self.environment.expressions.push(TypedExpression {
            id: id.clone(),
            kind,
            type_name,
            type_hint,
            modeling,
            operands,
            reads,
            source: span.clone(),
        });
        self.mappings.expressions.push(ExpressionMapping {
            expression_id: id.clone(),
            source: span,
            source_ast_ids: vec![node.id],
            lowered_ast_ids,
        });
        self.expression_by_ast.insert(node.id, id.clone());
        id
    }

    fn reference_expression(
        &mut self,
        node: &AstNode,
        reference: &ReferenceExpr,
    ) -> (
        ExpressionKind,
        Vec<String>,
        BTreeSet<String>,
        ModelingStatus,
    ) {
        let mut operands = Vec::new();
        if let Some(base) = &reference.base {
            operands.push(self.expression(base));
        }
        let kind = match &reference.access {
            ReferenceAccess::Global(inner) | ReferenceAccess::Member(inner) => {
                operands.push(self.expression(inner));
                ExpressionKind::MemberAccess {
                    member: inner.get_flat_reference_name().map(str::to_string),
                }
            }
            ReferenceAccess::Index(inner) => {
                operands.push(self.expression(inner));
                ExpressionKind::ArrayIndex
            }
            ReferenceAccess::Cast(inner) => {
                operands.push(self.expression(inner));
                ExpressionKind::Cast {
                    target_type: self
                        .annotations
                        .get_type_hint(node, self.index)
                        .map(|ty| ty.get_name().to_string()),
                }
            }
            ReferenceAccess::Deref => ExpressionKind::PointerDeref,
            ReferenceAccess::Address => ExpressionKind::AddressOf,
        };
        let mut reads = self.union_reads(&operands);
        if let Some(qualified) = self.annotations.get_qualified_name(node) {
            reads.insert(qualified.to_string());
        }
        let modeling = self.max_expression_modeling(&operands);
        (kind, operands, reads, modeling)
    }

    fn call_expression(
        &mut self,
        call: &CallStatement,
    ) -> (
        ExpressionKind,
        Vec<String>,
        BTreeSet<String>,
        ModelingStatus,
    ) {
        let callee = self
            .annotations
            .get_call_name(&call.operator)
            .map(str::to_string);
        let operands = call
            .parameters
            .as_deref()
            .map(|parameters| {
                parameters
                    .get_as_list()
                    .into_iter()
                    .map(|parameter| self.expression(parameter))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let effects = self.call_effects(callee.as_deref(), &operands);
        self.register_call_summary(callee.as_deref(), &effects);
        let mut reads = self.union_reads(&operands);
        reads.extend(effects.reads.iter().cloned());
        (
            ExpressionKind::Call { callee },
            operands,
            reads,
            effects.modeling,
        )
    }

    fn conversion_for(&self, left: &AstNode, right: &AstNode) -> Option<TypeConversion> {
        let from = self
            .annotations
            .get_type(right, self.index)?
            .get_name()
            .to_string();
        let to = self
            .annotations
            .get_type_hint(right, self.index)
            .or_else(|| self.annotations.get_type(left, self.index))?
            .get_name()
            .to_string();
        if from.eq_ignore_ascii_case(&to) {
            return None;
        }
        let from_bits = self
            .index
            .get_type_information_or_void(&from)
            .get_semantic_size(self.index);
        let to_bits = self
            .index
            .get_type_information_or_void(&to)
            .get_semantic_size(self.index);
        Some(TypeConversion {
            from,
            to,
            modeling: ModelingStatus::Exact,
            potentially_narrowing: to_bits < from_bits,
        })
    }

    fn call_effects(&self, callee: Option<&str>, operands: &[String]) -> CallEffects {
        let Some(callee) = callee else {
            return CallEffects {
                modeling: ModelingStatus::Opaque,
                reads: self.union_reads(operands),
                writes: BTreeSet::new(),
                external: true,
            };
        };
        let internal = self.index.find_implementation_by_name(callee).is_some();
        let mut writes = BTreeSet::new();
        for parameter in self.index.get_available_parameters(callee) {
            if parameter.get_declaration_type().is_by_ref() || parameter.is_output() {
                writes.insert(parameter.get_qualified_name().to_string());
            }
        }
        CallEffects {
            modeling: if internal {
                ModelingStatus::Conservative
            } else {
                ModelingStatus::Opaque
            },
            reads: self.union_reads(operands),
            writes,
            external: !internal,
        }
    }

    fn register_call_summary(&mut self, callee: Option<&str>, effects: &CallEffects) {
        let name = callee.unwrap_or("<unresolved>").to_string();
        if self
            .environment
            .calls
            .iter()
            .any(|call| call.callee == name)
        {
            return;
        }
        let kind = self
            .index
            .find_pou(&name)
            .map(|pou| {
                if pou.is_function_block() {
                    "function_block"
                } else if pou.is_function() {
                    "function"
                } else {
                    "pou"
                }
            })
            .unwrap_or("external")
            .to_string();
        self.environment.calls.push(CallSummary {
            callee: name,
            kind,
            modeling: effects.modeling.clone(),
            reads: effects.reads.clone(),
            writes: effects.writes.clone(),
            external: effects.external,
        });
    }

    fn for_condition_expression(&mut self, statement: &ForLoopStatement) -> String {
        let counter = self.expression(&statement.counter);
        let end = self.expression(&statement.end);
        if let Some(step) = &statement.by_step {
            let step = self.expression(step);
            let zero = self.synthetic_integer_expression(0, &statement.end.location);
            let positive = self.synthetic_binary_expression(
                ">",
                &step,
                &zero,
                "BOOL".to_string(),
                statement.end.location.clone(),
            );
            let le = self.synthetic_binary_expression(
                "<=",
                &counter,
                &end,
                "BOOL".to_string(),
                statement.end.location.clone(),
            );
            let ge = self.synthetic_binary_expression(
                ">=",
                &counter,
                &end,
                "BOOL".to_string(),
                statement.end.location.clone(),
            );
            let positive_path = self.synthetic_binary_expression(
                "AND",
                &positive,
                &le,
                "BOOL".to_string(),
                statement.end.location.clone(),
            );
            let not_positive = self.synthetic_unary_expression(
                "NOT",
                &positive,
                "BOOL",
                statement.end.location.clone(),
            );
            let negative_path = self.synthetic_binary_expression(
                "AND",
                &not_positive,
                &ge,
                "BOOL".to_string(),
                statement.end.location.clone(),
            );
            self.synthetic_binary_expression(
                "OR",
                &positive_path,
                &negative_path,
                "BOOL".to_string(),
                statement.end.location.clone(),
            )
        } else {
            self.synthetic_binary_expression(
                "<=",
                &counter,
                &end,
                "BOOL".to_string(),
                statement.end.location.clone(),
            )
        }
    }

    fn case_guard_expression(&mut self, selector: &str, label: &str, node: &AstNode) -> String {
        if matches!(node.get_stmt(), AstStatement::RangeStatement(_)) {
            let operands = self
                .environment
                .expressions
                .iter()
                .find(|expr| expr.id == label)
                .map(|expr| expr.operands.clone())
                .unwrap_or_default();
            if operands.len() == 2 {
                let ge = self.synthetic_binary_expression(
                    ">=",
                    selector,
                    &operands[0],
                    "BOOL".to_string(),
                    node.location.clone(),
                );
                let le = self.synthetic_binary_expression(
                    "<=",
                    selector,
                    &operands[1],
                    "BOOL".to_string(),
                    node.location.clone(),
                );
                return self.synthetic_binary_expression(
                    "AND",
                    &ge,
                    &le,
                    "BOOL".to_string(),
                    node.location.clone(),
                );
            }
        }
        self.synthetic_binary_expression(
            "=",
            selector,
            label,
            "BOOL".to_string(),
            node.location.clone(),
        )
    }

    fn synthetic_nor_expression(&mut self, guards: &[String], location: SourceLocation) -> String {
        if guards.is_empty() {
            return self.synthetic_boolean_expression(true, &location);
        }
        let mut combined = guards[0].clone();
        for guard in &guards[1..] {
            combined = self.synthetic_binary_expression(
                "OR",
                &combined,
                guard,
                "BOOL".to_string(),
                location.clone(),
            );
        }
        self.synthetic_unary_expression("NOT", &combined, "BOOL", location)
    }

    fn synthetic_binary_expression(
        &mut self,
        operator: &str,
        left: &str,
        right: &str,
        type_name: String,
        location: SourceLocation,
    ) -> String {
        self.synthetic_expression(
            ExpressionKind::Binary {
                operator: operator.to_string(),
            },
            vec![left.to_string(), right.to_string()],
            type_name,
            location,
        )
    }

    fn synthetic_unary_expression(
        &mut self,
        operator: &str,
        operand: &str,
        type_name: &str,
        location: SourceLocation,
    ) -> String {
        self.synthetic_expression(
            ExpressionKind::Unary {
                operator: operator.to_string(),
            },
            vec![operand.to_string()],
            type_name.to_string(),
            location,
        )
    }

    fn synthetic_integer_expression(&mut self, value: i128, location: &SourceLocation) -> String {
        self.synthetic_expression(
            ExpressionKind::Literal {
                value: LiteralValue::Integer(value.to_string()),
            },
            Vec::new(),
            "DINT".to_string(),
            location.clone(),
        )
    }

    fn synthetic_boolean_expression(&mut self, value: bool, location: &SourceLocation) -> String {
        self.synthetic_expression(
            ExpressionKind::Literal {
                value: LiteralValue::Boolean(value),
            },
            Vec::new(),
            "BOOL".to_string(),
            location.clone(),
        )
    }

    fn synthetic_expression(
        &mut self,
        kind: ExpressionKind,
        operands: Vec<String>,
        type_name: String,
        location: SourceLocation,
    ) -> String {
        let span = source_span(&location, self.root);
        let kind_name = expression_kind_name(&kind);
        let ordinal = self
            .environment
            .expressions
            .iter()
            .filter(|expression| expression_kind_name(&expression.kind) == kind_name)
            .count();
        let id = semantic_id(
            "expr",
            span.as_ref()
                .map(|s| s.file.as_str())
                .unwrap_or(&self.file_key),
            &self.qualified_name,
            span.as_ref(),
            kind_name,
            ordinal,
        );
        let reads = self.union_reads(&operands);
        let modeling = self.max_expression_modeling(&operands);
        self.environment.expressions.push(TypedExpression {
            id: id.clone(),
            kind,
            type_name,
            type_hint: None,
            modeling,
            operands,
            reads,
            source: span.clone(),
        });
        self.mappings.expressions.push(ExpressionMapping {
            expression_id: id.clone(),
            source: span,
            source_ast_ids: Vec::new(),
            lowered_ast_ids: Vec::new(),
        });
        id
    }

    fn expression_type(&self, node: &AstNode) -> String {
        self.annotations
            .get_type(node, self.index)
            .map(|ty| ty.get_name().to_string())
            .unwrap_or_else(|| "VOID".to_string())
    }

    fn expression_reads(&self, id: &str) -> BTreeSet<String> {
        self.environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)
            .map(|expression| expression.reads.clone())
            .unwrap_or_default()
    }

    fn expression_modeling_by_id(&self, id: &str) -> ModelingStatus {
        self.environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)
            .map(|expression| expression.modeling.clone())
            .unwrap_or(ModelingStatus::Opaque)
    }

    fn max_expression_modeling(&self, expressions: &[String]) -> ModelingStatus {
        expressions
            .iter()
            .fold(ModelingStatus::Exact, |status, id| {
                max_modeling(&status, &self.expression_modeling_by_id(id))
            })
    }

    fn union_reads(&self, expressions: &[String]) -> BTreeSet<String> {
        expressions
            .iter()
            .flat_map(|id| self.expression_reads(id))
            .collect()
    }

    fn expression_variable_name(&self, id: &str) -> Option<String> {
        let expression = self
            .environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)?;
        match &expression.kind {
            ExpressionKind::Variable { qualified_name } => Some(qualified_name.clone()),
            ExpressionKind::MemberAccess { .. }
            | ExpressionKind::ArrayIndex
            | ExpressionKind::PointerDeref
            | ExpressionKind::DirectAccess { .. } => expression.reads.iter().next().cloned(),
            _ => None,
        }
    }

    fn is_external_output_target(&self, id: &str) -> bool {
        let Some(expression) = self
            .environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)
        else {
            return false;
        };
        matches!(expression.kind, ExpressionKind::HardwareAccess)
            || self
                .expression_variable_name(id)
                .is_some_and(|name| self.symbol_roles.get(&name) == Some(&VariableRole::Output))
    }

    fn expression_complexity(&self, id: &str) -> usize {
        let Some(expression) = self
            .environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)
        else {
            return 0;
        };
        1 + expression
            .operands
            .iter()
            .map(|operand| self.expression_complexity(operand))
            .sum::<usize>()
    }

    fn expression_call_count(&self, id: &str) -> usize {
        let Some(expression) = self
            .environment
            .expressions
            .iter()
            .find(|expression| expression.id == id)
        else {
            return 0;
        };
        usize::from(matches!(expression.kind, ExpressionKind::Call { .. }))
            + expression
                .operands
                .iter()
                .map(|operand| self.expression_call_count(operand))
                .sum::<usize>()
    }

    fn add_node(
        &mut self,
        mut spec: NodeSpec,
        ordinal: usize,
        discriminator: Option<String>,
    ) -> String {
        let kind_name = node_kind_name(&spec.kind);
        let key = format!(
            "{}|{}|{}",
            kind_name,
            spec.source
                .as_ref()
                .map(|source| format!("{}:{}", source.start_offset, source.end_offset))
                .unwrap_or_else(|| "synthetic".to_string()),
            discriminator.unwrap_or_default()
        );
        let occurrence = self.node_ordinals.entry(key).or_insert(ordinal);
        let id = semantic_id(
            "node",
            spec.source
                .as_ref()
                .map(|source| source.file.as_str())
                .unwrap_or(&self.file_key),
            &self.qualified_name,
            spec.source.as_ref(),
            kind_name,
            *occurrence,
        );
        *occurrence += 1;
        let hazard_origin = spec.hazard.as_ref().and_then(|hazard| {
            self.mappings
                .expressions
                .iter()
                .find(|mapping| mapping.expression_id == hazard.expression_id)
                .cloned()
        });
        if let Some(mapping) = &hazard_origin {
            spec.source_ast_ids
                .extend(mapping.source_ast_ids.iter().copied());
            spec.source_ast_ids.sort_unstable();
            spec.source_ast_ids.dedup();
        }
        let lowered_ast_ids = spec
            .source
            .as_ref()
            .and_then(|source| self.lowered_facts.spans.get(source))
            .cloned()
            .filter(|ids| !ids.is_empty())
            .or_else(|| hazard_origin.map(|mapping| mapping.lowered_ast_ids))
            .unwrap_or_default();
        let origin = AstOrigin {
            source_ast_ids: spec.source_ast_ids.clone(),
            lowered_ast_ids: lowered_ast_ids.clone(),
        };
        self.graph.nodes.push(SemanticNode {
            id: id.clone(),
            kind: spec.kind,
            label: spec.label,
            source: spec.source.clone(),
            origin,
            outcome: spec.outcome,
            hazard: spec.hazard,
            features: spec.features,
        });
        self.mappings.nodes.push(NodeMapping {
            semantic_id: id.clone(),
            pou_qualified_name: self.qualified_name.clone(),
            source: spec.source,
            source_ast_ids: spec.source_ast_ids,
            lowered_ast_ids,
        });
        id
    }

    fn extend_node_lowered_ids(&mut self, semantic_id: &str, lowered_ids: &[usize]) {
        if lowered_ids.is_empty() {
            return;
        }
        if let Some(node) = self
            .graph
            .nodes
            .iter_mut()
            .find(|node| node.id == semantic_id)
        {
            node.origin.lowered_ast_ids.extend_from_slice(lowered_ids);
            node.origin.lowered_ast_ids.sort_unstable();
            node.origin.lowered_ast_ids.dedup();
        }
        if let Some(mapping) = self
            .mappings
            .nodes
            .iter_mut()
            .find(|mapping| mapping.semantic_id == semantic_id)
        {
            mapping.lowered_ast_ids.extend_from_slice(lowered_ids);
            mapping.lowered_ast_ids.sort_unstable();
            mapping.lowered_ast_ids.dedup();
        }
    }

    fn add_evaluation_edge(&mut self, from: &str, to: &str, guard: &str) {
        self.add_edge(
            from,
            to,
            EdgeKind::Evaluation,
            Some(guard.to_string()),
            None,
            None,
            false,
        );
        let guard_modeling = self.expression_modeling_by_id(guard);
        if let Some(target) = self.targets.iter_mut().find(|target| target.node_id == to) {
            target.modeling = max_modeling(&target.modeling, &guard_modeling);
        }
    }

    fn add_control_edge(
        &mut self,
        from: &str,
        to: &str,
        summary: TransferSummary,
        is_back_edge: bool,
    ) {
        self.add_edge(
            from,
            to,
            EdgeKind::ControlTransfer,
            None,
            Some(summary),
            None,
            is_back_edge,
        );
    }

    fn add_temporal_edge(&mut self, from: &str, to: String, summary: TemporalSummary) {
        self.add_edge(
            from,
            &to,
            EdgeKind::Temporal,
            None,
            None,
            Some(summary),
            true,
        );
    }

    #[allow(clippy::too_many_arguments)]
    fn add_edge(
        &mut self,
        from: &str,
        to: &str,
        kind: EdgeKind,
        guard: Option<String>,
        transfer: Option<TransferSummary>,
        temporal: Option<TemporalSummary>,
        is_back_edge: bool,
    ) {
        let id = semantic_id(
            "edge",
            &self.file_key,
            &self.qualified_name,
            None,
            &format!("{kind:?}:{from}:{to}"),
            self.edge_ordinal,
        );
        self.edge_ordinal += 1;
        self.graph.edges.push(SemanticEdge {
            id,
            from: from.to_string(),
            to: to.to_string(),
            kind,
            guard,
            transfer,
            temporal,
            is_back_edge,
        });
    }

    fn connect_points(&mut self, points: Vec<FlowPoint>, target: &str, is_back_edge: bool) {
        for point in points {
            self.add_control_edge(&point.node, target, point.summary, is_back_edge);
        }
    }

    fn add_target(
        &mut self,
        node_id: &str,
        kind: TargetKind,
        role: Option<OutcomeRole>,
        modeling: ModelingStatus,
    ) {
        let node = self.graph.nodes.iter().find(|node| node.id == node_id);
        let source = node.and_then(|node| node.source.clone());
        let id = semantic_id(
            "target",
            source
                .as_ref()
                .map(|source| source.file.as_str())
                .unwrap_or(&self.file_key),
            &self.qualified_name,
            source.as_ref(),
            &format!("{kind:?}:{node_id}"),
            0,
        );
        self.targets.push(SemanticTarget {
            id,
            node_id: node_id.to_string(),
            edge_ids: Vec::new(),
            kind,
            role,
            source,
            backward_dependencies: Vec::new(),
            modeling,
        });
    }

    fn finalize_target_edges(&mut self) {
        for target in &mut self.targets {
            let outgoing_back_edge = target.role == Some(OutcomeRole::LoopBack);
            let mut edge_ids = self
                .graph
                .edges
                .iter()
                .filter(|edge| {
                    if outgoing_back_edge {
                        edge.from == target.node_id && edge.is_back_edge
                    } else if target.role == Some(OutcomeRole::CycleEntry) {
                        edge.to == target.node_id && edge.kind == EdgeKind::ControlTransfer
                    } else {
                        edge.to == target.node_id && edge.kind != EdgeKind::Temporal
                    }
                })
                .map(|edge| edge.id.clone())
                .collect::<Vec<_>>();
            edge_ids.sort();
            edge_ids.dedup();
            target.edge_ids = edge_ids;
        }
    }

    fn add_diagnostic(
        &mut self,
        code: &str,
        severity: DiagnosticSeverity,
        message: &str,
        semantic_id: Option<String>,
        source: Option<SourceSpan>,
    ) {
        self.diagnostics.push(ModelDiagnostic {
            code: code.to_string(),
            severity,
            message: message.to_string(),
            pou: Some(self.qualified_name.clone()),
            semantic_id,
            source,
        });
    }

    fn finalize_def_use(&mut self) {
        let mut links = Vec::new();
        for edge in &self.graph.edges {
            let Some(summary) = &edge.transfer else {
                continue;
            };
            for operation in &summary.operations {
                if let TransferOperation::Assign {
                    target,
                    target_symbol,
                    value,
                    reads,
                    ..
                } = operation
                {
                    let Some(defined_symbol) = target_symbol
                        .clone()
                        .or_else(|| self.expression_variable_name(target))
                    else {
                        continue;
                    };
                    for used_symbol in reads {
                        links.push(DefUseLink {
                            defined_symbol: defined_symbol.clone(),
                            used_symbol: used_symbol.clone(),
                            definition_edge: edge.id.clone(),
                            use_expression: value.clone(),
                        });
                    }
                }
            }
        }
        links.sort_by(|a, b| {
            (
                &a.defined_symbol,
                &a.used_symbol,
                &a.definition_edge,
                &a.use_expression,
            )
                .cmp(&(
                    &b.defined_symbol,
                    &b.used_symbol,
                    &b.definition_edge,
                    &b.use_expression,
                ))
        });
        links.dedup_by(|a, b| {
            a.defined_symbol == b.defined_symbol
                && a.used_symbol == b.used_symbol
                && a.definition_edge == b.definition_edge
                && a.use_expression == b.use_expression
        });
        self.environment.def_use = links;
    }

    fn ensure_expression_types(&mut self) {
        let mut known = self
            .environment
            .types
            .iter()
            .map(|ty| ty.name.to_ascii_lowercase())
            .collect::<BTreeSet<_>>();
        let mut pending = self
            .environment
            .expressions
            .iter()
            .flat_map(|expression| {
                std::iter::once(expression.type_name.clone())
                    .chain(expression.type_hint.iter().cloned())
            })
            .collect::<BTreeSet<_>>();
        while let Some(type_name) = pending.pop_first() {
            if type_name == "VOID" || !known.insert(type_name.to_ascii_lowercase()) {
                continue;
            }
            if let Some(ty) = self.index.find_type(&type_name) {
                let model = type_model(self.index, ty, self.root);
                if let Some(element) = &model.element_type {
                    pending.insert(element.clone());
                }
                if let Some(pointer) = &model.pointer_target {
                    pending.insert(pointer.clone());
                }
                for field in &model.fields {
                    pending.insert(field.type_name.clone());
                }
                self.environment.types.push(model);
            }
        }
    }

    fn finalize_dependencies(&mut self) {
        let expression_reads = self
            .environment
            .expressions
            .iter()
            .map(|expression| (expression.id.clone(), expression.reads.clone()))
            .collect::<BTreeMap<_, _>>();
        let incoming = self.graph.edges.iter().fold(
            BTreeMap::<String, Vec<&SemanticEdge>>::new(),
            |mut map, edge| {
                map.entry(edge.to.clone()).or_default().push(edge);
                map
            },
        );
        let hazard_reads = self
            .graph
            .nodes
            .iter()
            .filter_map(|node| {
                node.hazard.as_ref().map(|hazard| {
                    (
                        node.id.clone(),
                        expression_reads
                            .get(&hazard.expression_id)
                            .cloned()
                            .unwrap_or_default(),
                    )
                })
            })
            .collect::<BTreeMap<_, _>>();
        let def_use = self.environment.def_use.iter().fold(
            BTreeMap::<String, BTreeSet<String>>::new(),
            |mut map, link| {
                map.entry(link.defined_symbol.clone())
                    .or_default()
                    .insert(link.used_symbol.clone());
                map
            },
        );
        for target in &mut self.targets {
            let mut dependencies = hazard_reads
                .get(&target.node_id)
                .cloned()
                .unwrap_or_default();
            let mut pending = vec![target.node_id.clone()];
            let mut visited_edges = BTreeSet::new();
            while let Some(node_id) = pending.pop() {
                for edge in incoming.get(&node_id).into_iter().flatten() {
                    if !visited_edges.insert(edge.id.clone()) {
                        continue;
                    }
                    if let Some(guard) = &edge.guard {
                        dependencies
                            .extend(expression_reads.get(guard).cloned().unwrap_or_default());
                    }
                    if let Some(summary) = &edge.transfer {
                        dependencies.extend(summary.reads.iter().cloned());
                    }
                    if let Some(temporal) = &edge.temporal {
                        dependencies.extend(temporal.persistent_symbols.iter().cloned());
                        // Stop at the scan boundary. Earlier-cycle control
                        // choices are planned explicitly by the multi-cycle
                        // planner rather than flattened into this local slice.
                        continue;
                    }
                    pending.push(edge.from.clone());
                }
            }
            let mut expansion = dependencies.iter().cloned().collect::<Vec<_>>();
            while let Some(symbol) = expansion.pop() {
                for used in def_use.get(&symbol).into_iter().flatten() {
                    if dependencies.insert(used.clone()) {
                        expansion.push(used.clone());
                    }
                }
            }
            target.backward_dependencies = dependencies.iter().cloned().collect();
            self.environment
                .target_dependencies
                .insert(target.id.clone(), target.backward_dependencies.clone());
        }
    }

    fn sort_model(&mut self) {
        self.environment
            .symbols
            .sort_by(|a, b| a.qualified_name.cmp(&b.qualified_name));
        self.environment.types.sort_by(|a, b| a.name.cmp(&b.name));
        self.environment
            .calls
            .sort_by(|a, b| a.callee.cmp(&b.callee));
        self.targets.sort_by(|a, b| a.id.cmp(&b.id));
        self.mappings
            .nodes
            .sort_by(|a, b| a.semantic_id.cmp(&b.semantic_id));
        self.mappings
            .expressions
            .sort_by(|a, b| a.expression_id.cmp(&b.expression_id));
    }
}

impl FlowPoint {
    fn new(node: String) -> Self {
        Self {
            node,
            summary: TransferSummary::default(),
            versions: BTreeMap::new(),
        }
    }
}

struct NodeSpec {
    kind: NodeKind,
    label: String,
    source: Option<SourceSpan>,
    source_ast_ids: Vec<usize>,
    outcome: Option<Outcome>,
    hazard: Option<Hazard>,
    features: StaticFeatures,
}

impl NodeSpec {
    fn synthetic(kind: NodeKind, label: &str) -> Self {
        Self {
            kind,
            label: label.to_string(),
            source: None,
            source_ast_ids: Vec::new(),
            outcome: None,
            hazard: None,
            features: StaticFeatures::default(),
        }
    }
}

fn apply_operation(
    summary: &mut TransferSummary,
    versions: &mut BTreeMap<String, u32>,
    mut operation: TransferOperation,
) {
    match &mut operation {
        TransferOperation::Assign {
            target,
            target_symbol,
            reads,
            modeling,
            version_before,
            version_after,
            ..
        } => {
            let symbol = target_symbol.clone().unwrap_or_else(|| target.clone());
            let before = *versions.get(&symbol).unwrap_or(&0);
            let after = before + 1;
            *version_before = before;
            *version_after = after;
            versions.insert(symbol.clone(), after);
            summary.reads.extend(reads.iter().cloned());
            summary.writes.insert(symbol.clone());
            summary.versions_in.entry(symbol.clone()).or_insert(before);
            summary.versions_out.insert(symbol, after);
            summary.modeling = max_modeling(&summary.modeling, modeling);
        }
        TransferOperation::Call { effects, .. } => {
            summary.reads.extend(effects.reads.iter().cloned());
            summary.writes.extend(effects.writes.iter().cloned());
            summary.modeling = max_modeling(&summary.modeling, &effects.modeling);
        }
        TransferOperation::Opaque { .. } => summary.modeling = ModelingStatus::Opaque,
        TransferOperation::Return { .. } | TransferOperation::Allocation { .. } => {}
    }
    summary.operations.push(operation);
}

fn max_modeling(a: &ModelingStatus, b: &ModelingStatus) -> ModelingStatus {
    std::cmp::max(a, b).clone()
}

fn source_span(location: &SourceLocation, root: &Path) -> Option<SourceSpan> {
    let range = location.to_range()?;
    let file = location.get_file_name()?;
    let path = Path::new(file);
    let relative = path.strip_prefix(root).unwrap_or(path);
    Some(SourceSpan {
        file: normalize_path(relative),
        start_offset: range.start,
        end_offset: range.end,
        start_line: location.get_line() + 1,
        start_column: location.get_column() + 1,
        end_line: location.get_line_end() + 1,
        end_column: location.get_column_end() + 1,
    })
}

fn pou_kind(kind: &PouType) -> PouKind {
    match kind {
        PouType::Function => PouKind::Function,
        PouType::FunctionBlock => PouKind::FunctionBlock,
        PouType::Program => PouKind::Program,
        PouType::Method { .. } => PouKind::Method,
        PouType::Action => PouKind::Action,
        PouType::Class => PouKind::Class,
        _ => PouKind::Other,
    }
}

fn variable_role(kind: VariableType, stateful: bool) -> VariableRole {
    match kind {
        VariableType::Input => VariableRole::Input,
        VariableType::Output => VariableRole::Output,
        VariableType::InOut => VariableRole::InOut,
        VariableType::Local if stateful => VariableRole::PersistentState,
        VariableType::Local => VariableRole::Local,
        VariableType::Temp => VariableRole::Temporary,
        VariableType::Return => VariableRole::Return,
        VariableType::Global => VariableRole::Global,
        VariableType::External => VariableRole::External,
        VariableType::Property => VariableRole::Property,
    }
}

fn type_model(index: &Index, ty: &plc::typesystem::DataType, root: &Path) -> IecType {
    let info = ty.get_type_information();
    let mut model = IecType {
        name: ty.get_name().to_string(),
        kind: TypeKind::Void,
        bit_width: info.get_size_in_bits(index).ok(),
        semantic_bit_width: Some(info.get_semantic_size(index)),
        signed: None,
        element_type: None,
        dimensions: Vec::new(),
        fields: Vec::new(),
        string_encoding: None,
        string_capacity: None,
        pointer_target: None,
        type_safe_pointer: None,
        source: source_span(&ty.location, root),
        modeling: ModelingStatus::Exact,
    };
    match info {
        DataTypeInformation::Integer {
            signed,
            semantic_size,
            ..
        } => {
            model.kind = if info.is_bool() {
                TypeKind::Boolean
            } else {
                TypeKind::Integer
            };
            model.signed = Some(*signed);
            model.semantic_bit_width = semantic_size.or(model.bit_width);
        }
        DataTypeInformation::Float { .. } => model.kind = TypeKind::Float,
        DataTypeInformation::Array {
            inner_type_name,
            dimensions,
            ..
        } => {
            model.kind = TypeKind::Array;
            model.element_type = Some(inner_type_name.clone());
            model.dimensions = dimensions
                .iter()
                .map(|dimension| ArrayDimension {
                    lower: dimension.start_offset.as_int_value(index).ok(),
                    upper: dimension.end_offset.as_int_value(index).ok(),
                })
                .collect();
        }
        DataTypeInformation::String { size, encoding } => {
            model.kind = TypeKind::String;
            model.string_encoding = Some(
                match encoding {
                    StringEncoding::Utf8 => "utf8",
                    StringEncoding::Utf16 => "utf16",
                }
                .to_string(),
            );
            model.string_capacity = size.as_int_value(index).ok();
        }
        DataTypeInformation::Struct { members, .. } => {
            model.kind = TypeKind::Struct;
            model.fields = members
                .iter()
                .map(|member| {
                    TypeField {
                        name: member.get_name().to_string(),
                        qualified_name: member.get_qualified_name().to_string(),
                        type_name: member.get_type_name().to_string(),
                        // Field participation is known semantically, but ABI
                        // alignment is target-specific and not exposed here.
                        offset_bits: None,
                    }
                })
                .collect();
        }
        DataTypeInformation::Pointer {
            inner_type_name,
            type_safe,
            ..
        } => {
            model.kind = TypeKind::Pointer;
            model.pointer_target = Some(inner_type_name.clone());
            model.type_safe_pointer = Some(*type_safe);
        }
        DataTypeInformation::Enum {
            referenced_type, ..
        } => {
            model.kind = TypeKind::Enum;
            model.element_type = Some(referenced_type.clone());
        }
        DataTypeInformation::SubRange {
            referenced_type, ..
        } => {
            model.kind = TypeKind::Subrange;
            model.element_type = Some(referenced_type.clone());
        }
        DataTypeInformation::Alias {
            referenced_type, ..
        } => {
            model.kind = TypeKind::Alias;
            model.element_type = Some(referenced_type.clone());
        }
        DataTypeInformation::Interface { .. } => model.kind = TypeKind::Interface,
        DataTypeInformation::Void => model.kind = TypeKind::Void,
        DataTypeInformation::Generic { .. } => model.kind = TypeKind::Generic,
    }
    model
}

fn literal_value(literal: &AstLiteral) -> LiteralValue {
    match literal {
        AstLiteral::Null => LiteralValue::Null,
        AstLiteral::Integer(value) => LiteralValue::Integer(value.to_string()),
        AstLiteral::Real(value) => LiteralValue::Real(value.clone()),
        AstLiteral::Bool(value) => LiteralValue::Boolean(*value),
        AstLiteral::String(value) if value.is_wide => LiteralValue::WideString(value.value.clone()),
        AstLiteral::String(value) => LiteralValue::String(value.value.clone()),
        AstLiteral::Time(value) => LiteralValue::DurationNanos(value.value()),
        AstLiteral::Date(value) => LiteralValue::DateNanos(value.value().ok()),
        AstLiteral::TimeOfDay(value) => LiteralValue::TimeOfDayNanos(value.value().ok()),
        AstLiteral::DateAndTime(value) => LiteralValue::DateTimeNanos(value.value().ok()),
        AstLiteral::Array(_) => LiteralValue::Array,
    }
}

fn case_labels(condition: &AstNode) -> Vec<&AstNode> {
    match condition.get_stmt() {
        AstStatement::ExpressionList(items) => items.iter().collect(),
        AstStatement::CaseCondition(inner) => case_labels(inner),
        _ => vec![condition],
    }
}

fn hazards_for_expression(expression: &TypedExpression, types: &[IecType]) -> Vec<HazardCandidate> {
    let mut hazards = Vec::new();
    let candidate = match &expression.kind {
        ExpressionKind::Binary { operator } if operator == "/" => Some((
            HazardKind::Division,
            "division denominator may be zero",
            ModelingStatus::Exact,
        )),
        ExpressionKind::Binary { operator } if operator.eq_ignore_ascii_case("MOD") => Some((
            HazardKind::Modulo,
            "modulo denominator may be zero",
            ModelingStatus::Exact,
        )),
        ExpressionKind::Binary { operator } if matches!(operator.as_str(), "+" | "-" | "*") => {
            let pointer_arithmetic = types.iter().any(|ty| {
                ty.name.eq_ignore_ascii_case(&expression.type_name) && ty.kind == TypeKind::Pointer
            });
            Some(if pointer_arithmetic {
                (
                    HazardKind::ArithmeticBoundary,
                    "pointer arithmetic may leave the valid referenced object",
                    ModelingStatus::Conservative,
                )
            } else {
                (
                    HazardKind::ArithmeticBoundary,
                    "finite-width arithmetic may cross an IEC type boundary",
                    ModelingStatus::Exact,
                )
            })
        }
        ExpressionKind::Binary { operator } if operator == "**" => Some((
            HazardKind::ArithmeticBoundary,
            "exponentiation may cross an IEC type boundary",
            ModelingStatus::Conservative,
        )),
        ExpressionKind::ArrayIndex => Some((
            HazardKind::ArrayAccess,
            "array index requires bounds validation",
            ModelingStatus::Exact,
        )),
        ExpressionKind::PointerDeref => Some((
            HazardKind::PointerAccess,
            "pointer dereference requires validity and lifetime constraints",
            ModelingStatus::Exact,
        )),
        ExpressionKind::Cast { .. } => Some((
            HazardKind::DangerousConversion,
            "explicit conversion may narrow or reinterpret a value",
            ModelingStatus::Conservative,
        )),
        _ => None,
    };
    if let Some((kind, detail, modeling)) = candidate {
        hazards.push(HazardCandidate {
            kind,
            expression_id: expression.id.clone(),
            source: expression.source.clone(),
            detail: detail.to_string(),
            modeling: max_modeling(&modeling, &expression.modeling),
        });
    }
    hazards
}

fn lowered_expression_key(node: &AstNode) -> Option<String> {
    match node.get_stmt() {
        AstStatement::BinaryExpression(expression) => {
            Some(format!("binary:{}", expression.operator))
        }
        AstStatement::UnaryExpression(expression) => Some(format!("unary:{}", expression.operator)),
        AstStatement::ReferenceExpr(reference) => Some(match reference.access {
            ReferenceAccess::Index(_) => "array_index".to_string(),
            ReferenceAccess::Cast(_) => "cast".to_string(),
            ReferenceAccess::Deref => "pointer_deref".to_string(),
            ReferenceAccess::Address => "address_of".to_string(),
            ReferenceAccess::Global(_) | ReferenceAccess::Member(_) => "member_access".to_string(),
        }),
        AstStatement::CallStatement(_) => Some("call".to_string()),
        AstStatement::ParenExpression(inner) => lowered_expression_key(inner),
        _ => None,
    }
}

fn semantic_expression_key(kind: &ExpressionKind) -> String {
    match kind {
        ExpressionKind::Binary { operator } => format!("binary:{operator}"),
        ExpressionKind::Unary { operator } => format!("unary:{operator}"),
        ExpressionKind::ArrayIndex => "array_index".to_string(),
        ExpressionKind::Cast { .. } => "cast".to_string(),
        ExpressionKind::PointerDeref => "pointer_deref".to_string(),
        ExpressionKind::AddressOf => "address_of".to_string(),
        ExpressionKind::MemberAccess { .. } | ExpressionKind::Variable { .. } => {
            "member_access".to_string()
        }
        ExpressionKind::Call { .. } => "call".to_string(),
        _ => expression_kind_name(kind).to_string(),
    }
}

fn expression_kind_name(kind: &ExpressionKind) -> &'static str {
    match kind {
        ExpressionKind::Literal { .. } => "literal",
        ExpressionKind::Variable { .. } => "variable",
        ExpressionKind::Binary { .. } => "binary",
        ExpressionKind::Unary { .. } => "unary",
        ExpressionKind::Call { .. } => "call",
        ExpressionKind::ArrayIndex => "array_index",
        ExpressionKind::MemberAccess { .. } => "member_access",
        ExpressionKind::PointerDeref => "pointer_deref",
        ExpressionKind::AddressOf => "address_of",
        ExpressionKind::Cast { .. } => "cast",
        ExpressionKind::DirectAccess { .. } => "direct_access",
        ExpressionKind::HardwareAccess => "hardware_access",
        ExpressionKind::Range => "range",
        ExpressionKind::List => "list",
        ExpressionKind::CaseCondition => "case_condition",
        ExpressionKind::Opaque { .. } => "opaque",
    }
}

fn node_kind_name(kind: &NodeKind) -> &'static str {
    match kind {
        NodeKind::Entry => "entry",
        NodeKind::Exit => "exit",
        NodeKind::CycleEntry => "cycle_entry",
        NodeKind::CycleExit => "cycle_exit",
        NodeKind::Predicate(PredicateKind::If) => "if_predicate",
        NodeKind::Predicate(PredicateKind::Elsif) => "elsif_predicate",
        NodeKind::Predicate(PredicateKind::Case) => "case_predicate",
        NodeKind::Predicate(PredicateKind::While) => "while_predicate",
        NodeKind::Predicate(PredicateKind::Repeat) => "repeat_predicate",
        NodeKind::Predicate(PredicateKind::For) => "for_predicate",
        NodeKind::Outcome => "outcome",
        NodeKind::LoopLatch => "loop_latch",
        NodeKind::LoopControl => "loop_control",
        NodeKind::Hazard => "hazard",
        NodeKind::PropertyViolation => "property_violation",
    }
}

fn ast_kind(statement: &AstStatement) -> &'static str {
    match statement {
        AstStatement::EmptyStatement(_) => "empty",
        AstStatement::DefaultValue(_) => "default_value",
        AstStatement::Literal(_) => "literal",
        AstStatement::MultipliedStatement(_) => "multiplied_statement",
        AstStatement::ReferenceExpr(_) => "reference",
        AstStatement::Identifier(_) => "identifier",
        AstStatement::Super(_) => "super",
        AstStatement::This => "this",
        AstStatement::DirectAccess(_) => "direct_access",
        AstStatement::HardwareAccess(_) => "hardware_access",
        AstStatement::BinaryExpression(_) => "binary_expression",
        AstStatement::UnaryExpression(_) => "unary_expression",
        AstStatement::ExpressionList(_) => "expression_list",
        AstStatement::ParenExpression(_) => "paren_expression",
        AstStatement::RangeStatement(_) => "range",
        AstStatement::VlaRangeStatement => "vla_range",
        AstStatement::Assignment(_) => "assignment",
        AstStatement::OutputAssignment(_) => "output_assignment",
        AstStatement::RefAssignment(_) => "reference_assignment",
        AstStatement::CallStatement(_) => "call",
        AstStatement::ControlStatement(_) => "control",
        AstStatement::CaseCondition(_) => "case_condition",
        AstStatement::ExitStatement(_) => "exit",
        AstStatement::ContinueStatement(_) => "continue",
        AstStatement::ReturnStatement(_) => "return",
        AstStatement::JumpStatement(_) => "jump",
        AstStatement::LabelStatement(_) => "label",
        AstStatement::AllocationStatement(_) => "allocation",
    }
}

fn pou_statistics(model: &PouSemanticModel) -> ModelStatistics {
    let diagnostics = &model.diagnostics;
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
        error_count: diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == DiagnosticSeverity::Error)
            .count(),
        warning_count: diagnostics
            .iter()
            .filter(|diagnostic| diagnostic.severity == DiagnosticSeverity::Warning)
            .count(),
    }
}

pub fn recompute_statistics(model: &mut ProjectSemanticModel) {
    let mut statistics = ModelStatistics {
        pou_count: model.pous.len(),
        ..Default::default()
    };
    for pou in &model.pous {
        statistics.node_count += pou.graph.nodes.len();
        statistics.edge_count += pou.graph.edges.len();
        statistics.target_count += pou.targets.len();
        statistics.predicate_count += pou.statistics.predicate_count;
        statistics.loop_count += pou.statistics.loop_count;
        statistics.persistent_state_count += pou.statistics.persistent_state_count;
        statistics.conservative_summary_count += pou.statistics.conservative_summary_count;
        statistics.opaque_summary_count += pou.statistics.opaque_summary_count;
        statistics.error_count += pou.statistics.error_count;
        statistics.warning_count += pou.statistics.warning_count;
    }
    model.statistics = statistics;
}
