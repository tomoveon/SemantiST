use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

pub const SCHEMA_VERSION: &str = "semantist.stg/1.2.0";

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProjectSemanticModel {
    pub schema_version: String,
    pub metadata: BuildMetadata,
    pub pous: Vec<PouSemanticModel>,
    pub diagnostics: Vec<ModelDiagnostic>,
    pub statistics: ModelStatistics,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct BuildMetadata {
    pub generator: String,
    pub generator_version: String,
    pub rusty_version: String,
    pub rusty_revision: String,
    pub project_root: String,
    pub inputs: Vec<InputMetadata>,
    pub deterministic: bool,
    pub semantic_sources: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct InputMetadata {
    pub relative_path: String,
    pub sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PouSemanticModel {
    pub model_id: String,
    pub pou: PouDescriptor,
    pub graph: SemanticGraph,
    pub environment: SemanticEnvironment,
    pub mappings: SemanticMappings,
    pub targets: Vec<SemanticTarget>,
    pub diagnostics: Vec<ModelDiagnostic>,
    pub statistics: PouStatistics,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PouDescriptor {
    pub name: String,
    pub qualified_name: String,
    pub kind: PouKind,
    pub source: SourceSpan,
    pub stateful: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PouKind {
    Function,
    FunctionBlock,
    Program,
    Method,
    Action,
    Class,
    Other,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticGraph {
    pub nodes: Vec<SemanticNode>,
    pub edges: Vec<SemanticEdge>,
    pub entry_id: String,
    pub exit_id: String,
    pub cycle_entry_id: Option<String>,
    pub cycle_exit_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SemanticNode {
    pub id: String,
    pub kind: NodeKind,
    pub label: String,
    pub source: Option<SourceSpan>,
    pub origin: AstOrigin,
    pub outcome: Option<Outcome>,
    pub hazard: Option<Hazard>,
    pub features: StaticFeatures,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "category", content = "kind", rename_all = "snake_case")]
pub enum NodeKind {
    Entry,
    Exit,
    CycleEntry,
    CycleExit,
    Predicate(PredicateKind),
    Outcome,
    LoopLatch,
    LoopControl,
    Hazard,
    PropertyViolation,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PredicateKind {
    If,
    Elsif,
    Case,
    While,
    Repeat,
    For,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Outcome {
    pub predicate_id: Option<String>,
    pub role: OutcomeRole,
    pub ordinal: usize,
    pub value: OutcomeValue,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OutcomeRole {
    True,
    False,
    Else,
    CaseLabel,
    CaseDefault,
    LoopEnter,
    LoopContinue,
    LoopExit,
    LoopBack,
    ExplicitExit,
    ExplicitContinue,
    Return,
    CycleEntry,
    CycleExit,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum OutcomeValue {
    Boolean(bool),
    Expression(String),
    Default,
    Structural,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SemanticEdge {
    pub id: String,
    pub from: String,
    pub to: String,
    pub kind: EdgeKind,
    pub guard: Option<String>,
    pub transfer: Option<TransferSummary>,
    pub temporal: Option<TemporalSummary>,
    pub is_back_edge: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EdgeKind {
    Evaluation,
    ControlTransfer,
    Temporal,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TransferSummary {
    pub modeling: ModelingStatus,
    pub operations: Vec<TransferOperation>,
    pub reads: BTreeSet<String>,
    pub writes: BTreeSet<String>,
    pub versions_in: BTreeMap<String, u32>,
    pub versions_out: BTreeMap<String, u32>,
    pub unmodified: HoldSemantics,
}

impl Default for TransferSummary {
    fn default() -> Self {
        Self {
            modeling: ModelingStatus::Exact,
            operations: Vec::new(),
            reads: BTreeSet::new(),
            writes: BTreeSet::new(),
            versions_in: BTreeMap::new(),
            versions_out: BTreeMap::new(),
            unmodified: HoldSemantics::Implicit,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum TransferOperation {
    Assign {
        target: String,
        target_symbol: Option<String>,
        value: String,
        reads: BTreeSet<String>,
        modeling: ModelingStatus,
        version_before: u32,
        version_after: u32,
        conversion: Option<TypeConversion>,
        source: SourceSpan,
    },
    Call {
        callee: Option<String>,
        arguments: Vec<String>,
        effects: CallEffects,
        source: SourceSpan,
    },
    Return {
        source: SourceSpan,
    },
    Allocation {
        name: String,
        type_name: String,
        source: SourceSpan,
    },
    Opaque {
        ast_kind: String,
        reason: String,
        source: Option<SourceSpan>,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TypeConversion {
    pub from: String,
    pub to: String,
    pub modeling: ModelingStatus,
    pub potentially_narrowing: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CallEffects {
    pub modeling: ModelingStatus,
    pub reads: BTreeSet<String>,
    pub writes: BTreeSet<String>,
    pub external: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TemporalSummary {
    pub modeling: ModelingStatus,
    pub persistent_symbols: Vec<String>,
    pub rule: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum ModelingStatus {
    Exact,
    Conservative,
    Opaque,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HoldSemantics {
    Implicit,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct StaticFeatures {
    pub nesting_depth: usize,
    pub condition_complexity: usize,
    pub input_dependencies: usize,
    pub state_dependencies: usize,
    pub loop_id: Option<String>,
    pub call_complexity: usize,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticEnvironment {
    pub symbols: Vec<Symbol>,
    pub types: Vec<IecType>,
    pub expressions: Vec<TypedExpression>,
    pub def_use: Vec<DefUseLink>,
    pub target_dependencies: BTreeMap<String, Vec<String>>,
    pub calls: Vec<CallSummary>,
    pub persistent_state: Option<PersistentState>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Symbol {
    pub id: String,
    pub name: String,
    pub qualified_name: String,
    pub type_name: String,
    pub role: VariableRole,
    pub constant: bool,
    pub configuration_source: Option<ConfigurationSource>,
    pub retain: bool,
    pub by_reference: bool,
    pub source: SourceSpan,
    pub initial_value: Option<String>,
    pub layout_offset_bits: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ConfigurationSource {
    IecConstant,
    VarInputConstantAnnotation,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VariableRole {
    Input,
    Output,
    InOut,
    Local,
    Temporary,
    PersistentState,
    Return,
    Global,
    External,
    Property,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IecType {
    pub name: String,
    pub kind: TypeKind,
    pub bit_width: Option<u32>,
    pub semantic_bit_width: Option<u32>,
    pub signed: Option<bool>,
    pub element_type: Option<String>,
    pub dimensions: Vec<ArrayDimension>,
    pub fields: Vec<TypeField>,
    pub string_encoding: Option<String>,
    pub string_capacity: Option<i64>,
    pub pointer_target: Option<String>,
    pub type_safe_pointer: Option<bool>,
    pub source: Option<SourceSpan>,
    pub modeling: ModelingStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TypeKind {
    Integer,
    Float,
    Boolean,
    Array,
    String,
    Struct,
    Pointer,
    Enum,
    Subrange,
    Alias,
    Interface,
    Void,
    Generic,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ArrayDimension {
    pub lower: Option<i64>,
    pub upper: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TypeField {
    pub name: String,
    pub qualified_name: String,
    pub type_name: String,
    pub offset_bits: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TypedExpression {
    pub id: String,
    pub kind: ExpressionKind,
    pub type_name: String,
    pub type_hint: Option<String>,
    pub modeling: ModelingStatus,
    pub operands: Vec<String>,
    pub reads: BTreeSet<String>,
    pub source: Option<SourceSpan>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExpressionKind {
    Literal { value: LiteralValue },
    Variable { qualified_name: String },
    Binary { operator: String },
    Unary { operator: String },
    Call { callee: Option<String> },
    ArrayIndex,
    MemberAccess { member: Option<String> },
    PointerDeref,
    AddressOf,
    Cast { target_type: Option<String> },
    DirectAccess { width_bits: u64 },
    HardwareAccess,
    Range,
    List,
    CaseCondition,
    Opaque { ast_kind: String },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum LiteralValue {
    Null,
    Integer(String),
    Real(String),
    Boolean(bool),
    String(String),
    WideString(String),
    DurationNanos(i64),
    DateNanos(Option<i64>),
    TimeOfDayNanos(Option<i64>),
    DateTimeNanos(Option<i64>),
    Array,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DefUseLink {
    pub defined_symbol: String,
    pub used_symbol: String,
    pub definition_edge: String,
    pub use_expression: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CallSummary {
    pub callee: String,
    pub kind: String,
    pub modeling: ModelingStatus,
    pub reads: BTreeSet<String>,
    pub writes: BTreeSet<String>,
    pub external: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PersistentState {
    pub type_name: String,
    pub symbols: Vec<String>,
    pub cycle_inputs: Vec<String>,
    pub inout_symbols: Vec<String>,
    pub configuration_symbols: Vec<String>,
    pub initialization: BTreeMap<String, Option<String>>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SemanticMappings {
    pub nodes: Vec<NodeMapping>,
    pub expressions: Vec<ExpressionMapping>,
    pub future_ir_locations: BTreeMap<String, Vec<IrLocation>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AstOrigin {
    pub source_ast_ids: Vec<usize>,
    pub lowered_ast_ids: Vec<usize>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct NodeMapping {
    pub semantic_id: String,
    pub pou_qualified_name: String,
    pub source: Option<SourceSpan>,
    pub source_ast_ids: Vec<usize>,
    pub lowered_ast_ids: Vec<usize>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ExpressionMapping {
    pub expression_id: String,
    pub source: Option<SourceSpan>,
    pub source_ast_ids: Vec<usize>,
    pub lowered_ast_ids: Vec<usize>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IrLocation {
    pub module: String,
    pub function: String,
    pub block: Option<String>,
    pub instruction: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct SourceSpan {
    pub file: String,
    pub start_offset: usize,
    pub end_offset: usize,
    pub start_line: usize,
    pub start_column: usize,
    pub end_line: usize,
    pub end_column: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SemanticTarget {
    pub id: String,
    pub node_id: String,
    pub edge_ids: Vec<String>,
    pub kind: TargetKind,
    pub role: Option<OutcomeRole>,
    pub source: Option<SourceSpan>,
    pub backward_dependencies: Vec<String>,
    pub modeling: ModelingStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TargetKind {
    BranchOutcome,
    LoopOutcome,
    CaseOutcome,
    ControlTransfer,
    Cycle,
    HazardReach,
    HazardViolation,
    ExternalOutput,
    PropertyViolation,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Hazard {
    pub kind: HazardKind,
    pub expression_id: String,
    pub modeling: ModelingStatus,
    pub detail: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HazardKind {
    Division,
    Modulo,
    ArrayAccess,
    PointerAccess,
    DangerousConversion,
    ArithmeticBoundary,
    ExternalOutput,
    UserProperty,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticSeverity {
    Error,
    Warning,
    Info,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ModelDiagnostic {
    pub code: String,
    pub severity: DiagnosticSeverity,
    pub message: String,
    pub pou: Option<String>,
    pub semantic_id: Option<String>,
    pub source: Option<SourceSpan>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ModelStatistics {
    pub pou_count: usize,
    pub node_count: usize,
    pub edge_count: usize,
    pub target_count: usize,
    pub predicate_count: usize,
    pub loop_count: usize,
    pub persistent_state_count: usize,
    pub conservative_summary_count: usize,
    pub opaque_summary_count: usize,
    pub error_count: usize,
    pub warning_count: usize,
}

pub type PouStatistics = ModelStatistics;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ArtifactManifest {
    pub schema_version: String,
    pub model: PathBuf,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostics: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub statistics: Option<PathBuf>,
    pub dot_files: Vec<PathBuf>,
    pub codegen_map: PathBuf,
    pub runtime_ids: PathBuf,
    pub model_sha256: String,
}
