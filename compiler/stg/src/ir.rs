use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context as _, Result};
use inkwell::context::Context;
use inkwell::intrinsics::Intrinsic;
use inkwell::memory_buffer::MemoryBuffer;
use inkwell::module::Module;
use inkwell::values::{
    AnyValue, AsValueRef, BasicMetadataValueEnum, BasicValue, BasicValueEnum, FunctionValue, InstructionOpcode,
    InstructionValue, Operand,
};
use inkwell::{FloatPredicate, IntPredicate};
use llvm_sys::core::{LLVMGetCalledValue, LLVMGetNumSuccessors, LLVMGetSuccessor,
                     LLVMGetValueName2, LLVMSetSuccessor};
use serde::{Deserialize, Serialize};

use crate::instrumentation::{RuntimeIdTable, ViolationPredicate, LLVM_METADATA_KIND};

pub const IR_MAPPING_SCHEMA: &str = "semantist.ir-mapping/1.0.0";

#[derive(Clone, Debug)]
pub struct InstrumentOptions {
    pub input_ir: PathBuf,
    pub output_ir: PathBuf,
    pub runtime_ids: PathBuf,
    pub mapping_output: PathBuf,
    pub diagnostics_output: Option<PathBuf>,
}

#[derive(Clone, Debug, Deserialize)]
struct MetadataPayload {
    schema_version: String,
    pou: String,
    lowered_ast_id: Option<usize>,
    events: Vec<MetadataEvent>,
}

#[derive(Clone, Debug, Deserialize)]
struct MetadataEvent {
    runtime_id: u32,
    stable_id: String,
    semantic_edge_ids: Vec<String>,
    kind: String,
    role: Option<String>,
    hazard: Option<String>,
    #[serde(default)]
    violation_predicate: Option<ViolationPredicate>,
    successor_hint: Option<u32>,
    modeling: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IrMappingArtifact {
    pub schema_version: String,
    pub input_ir: String,
    pub output_ir: String,
    pub mappings: Vec<IrSemanticMapping>,
    pub diagnostics: Vec<IrInstrumentationDiagnostic>,
    pub statistics: IrInstrumentationStatistics,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IrSemanticMapping {
    pub stable_id: String,
    pub runtime_id: u32,
    pub semantic_edge_ids: Vec<String>,
    pub target_kind: String,
    pub role: Option<String>,
    pub hazard: Option<String>,
    pub pou: String,
    pub lowered_ast_id: Option<usize>,
    pub function: String,
    pub source_block: String,
    pub source_instruction_ordinal: usize,
    pub successor_index: Option<u32>,
    pub destination_block: Option<String>,
    pub probe_block: Option<String>,
    pub mapping_kind: IrMappingKind,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum IrMappingKind {
    CfgEdge,
    Terminal,
    HazardReach,
    HazardViolation,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IrInstrumentationDiagnostic {
    pub code: String,
    pub message: String,
    pub stable_id: Option<String>,
    pub function: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct IrInstrumentationStatistics {
    pub metadata_instruction_count: usize,
    pub target_count: usize,
    pub mapped_target_count: usize,
    pub unmapped_target_count: usize,
    pub cfg_edge_mapping_count: usize,
    pub terminal_mapping_count: usize,
    pub hazard_reach_count: usize,
    pub hazard_violation_count: usize,
    pub probe_block_count: usize,
}

struct WorkItem<'ctx> {
    function: FunctionValue<'ctx>,
    instruction: InstructionValue<'ctx>,
    source_block: String,
    instruction_ordinal: usize,
    payload: MetadataPayload,
}

pub fn instrument(options: &InstrumentOptions) -> Result<IrMappingArtifact> {
    let runtime_ids: RuntimeIdTable = serde_json::from_slice(
        &std::fs::read(&options.runtime_ids)
            .with_context(|| format!("cannot read {}", options.runtime_ids.display()))?,
    )?;
    let known_runtime_ids = runtime_ids
        .entries
        .iter()
        .map(|entry| entry.runtime_id)
        .collect::<BTreeSet<_>>();

    let context = Context::create();
    let buffer = MemoryBuffer::create_from_file(&options.input_ir)
        .map_err(|error| anyhow::anyhow!("{}", error.to_string()))?;
    let module = context
        .create_module_from_ir(buffer)
        .map_err(|error| anyhow::anyhow!("{}", error.to_string()))?;
    let metadata_kind = context.get_kind_id(LLVM_METADATA_KIND);
    let work = collect_work(&module, metadata_kind)?;

    let hit_function = get_or_add_hit_function(&context, &module);
    let hazard_function = get_or_add_hazard_function(&context, &module);
    let mut mappings = Vec::new();
    let mut diagnostics = Vec::new();

    for item in work {
        if item.payload.schema_version != "semantist.llvm-semantic/1.0.0" {
            diagnostics.push(IrInstrumentationDiagnostic {
                code: "STG-IR-W001".to_string(),
                message: format!(
                    "unsupported LLVM semantic metadata schema {}",
                    item.payload.schema_version
                ),
                stable_id: None,
                function: Some(function_name(item.function)),
            });
            continue;
        }
        item.payload.events.iter().for_each(|event| {
            if !known_runtime_ids.contains(&event.runtime_id) {
                diagnostics.push(IrInstrumentationDiagnostic {
                    code: "STG-IR-E002".to_string(),
                    message: format!(
                        "metadata references unknown runtime ID {}",
                        event.runtime_id
                    ),
                    stable_id: Some(event.stable_id.clone()),
                    function: Some(function_name(item.function)),
                });
            }
        });

        let successor_count = if item.instruction.is_terminator() {
            (unsafe { LLVMGetNumSuccessors(item.instruction.as_value_ref()) }) as u32
        } else {
            0
        };
        if successor_count > 0 {
            instrument_successors(
                &context,
                item,
                successor_count,
                hit_function,
                &mut mappings,
                &mut diagnostics,
            )?;
        } else if item
            .payload
            .events
            .iter()
            .any(|event| event.kind.starts_with("hazard_") || event.kind == "external_output")
        {
            instrument_hazard(
                &context,
                &module,
                item,
                hit_function,
                hazard_function,
                &mut mappings,
                &mut diagnostics,
            )?;
        } else {
            instrument_terminal(&context, item, hit_function, &mut mappings)?;
        }
    }
    instrument_unanchored_float_calls(
        &context, &module, &runtime_ids, hit_function, hazard_function,
        &mut mappings, &mut diagnostics,
    )?;

    module.verify().map_err(|error| {
        anyhow::anyhow!("instrumented LLVM module is invalid: {}", error.to_string())
    })?;
    module
        .print_to_file(&options.output_ir)
        .map_err(|error| anyhow::anyhow!("{}", error.to_string()))?;

    mappings.sort_by(|left, right| {
        (
            left.runtime_id,
            &left.function,
            &left.source_block,
            left.successor_index,
        )
            .cmp(&(
                right.runtime_id,
                &right.function,
                &right.source_block,
                right.successor_index,
            ))
    });
    diagnostics.sort_by(|left, right| {
        (&left.code, &left.stable_id, &left.function).cmp(&(
            &right.code,
            &right.stable_id,
            &right.function,
        ))
    });

    let mapped = mappings
        .iter()
        .map(|mapping| mapping.runtime_id)
        .collect::<BTreeSet<_>>();
    for entry in &runtime_ids.entries {
        if !mapped.contains(&entry.runtime_id) {
            diagnostics.push(IrInstrumentationDiagnostic {
                code: "STG-IR-E003".to_string(),
                message: "STG target was not mapped to a final LLVM location".to_string(),
                stable_id: Some(entry.stable_id.clone()),
                function: Some(entry.pou.clone()),
            });
        }
    }

    let statistics = statistics(&runtime_ids, &mappings, &mapped);
    let artifact = IrMappingArtifact {
        schema_version: IR_MAPPING_SCHEMA.to_string(),
        input_ir: normalized_path(&options.input_ir),
        output_ir: normalized_path(&options.output_ir),
        mappings,
        diagnostics: diagnostics.clone(),
        statistics,
    };
    write_json(&options.mapping_output, &artifact)?;
    if let Some(diagnostics_output) = options.diagnostics_output.as_ref() {
        write_json(diagnostics_output, &diagnostics)?;
    }
    Ok(artifact)
}

fn called_function_name(instruction: InstructionValue<'_>) -> Option<String> {
    if instruction.get_opcode() != InstructionOpcode::Call {
        return None;
    }
    let called = unsafe { LLVMGetCalledValue(instruction.as_value_ref()) };
    if called.is_null() {
        return None;
    }
    let mut length = 0usize;
    let raw = unsafe { LLVMGetValueName2(called, &mut length) };
    if raw.is_null() || length == 0 {
        return None;
    }
    Some(String::from_utf8_lossy(unsafe {
        std::slice::from_raw_parts(raw.cast::<u8>(), length)
    }).into_owned())
}

fn instrument_unanchored_float_calls<'ctx>(
    context: &'ctx Context,
    module: &Module<'ctx>,
    runtime_ids: &RuntimeIdTable,
    hit_function: FunctionValue<'ctx>,
    hazard_function: FunctionValue<'ctx>,
    mappings: &mut Vec<IrSemanticMapping>,
    diagnostics: &mut Vec<IrInstrumentationDiagnostic>,
) -> Result<()> {
    let mapped = mappings.iter().map(|entry| entry.runtime_id).collect::<BTreeSet<_>>();
    let mut groups = BTreeMap::<(String, String, String), Vec<_>>::new();
    for entry in &runtime_ids.entries {
        if mapped.contains(&entry.runtime_id)
            || !matches!(entry.violation_predicate.as_ref(), Some(ViolationPredicate::FloatToInteger { .. }))
            || !matches!(&entry.kind, crate::model::TargetKind::HazardReach
                | crate::model::TargetKind::HazardViolation) {
            continue;
        }
        if let Some(callee) = &entry.source_callee {
            groups.entry((entry.pou.clone(), entry.node_id.clone(), callee.clone()))
                .or_default().push(entry);
        }
    }
    for ((pou, _, callee), entries) in groups {
        let function = module.get_functions().find(|function|
            function_name(*function).eq_ignore_ascii_case(&pou));
        let mut candidates = Vec::new();
        if let Some(function) = function {
            for block in function.get_basic_blocks() {
                let mut ordinal = 0usize;
                let mut cursor = block.get_first_instruction();
                while let Some(instruction) = cursor {
                    cursor = instruction.get_next_instruction();
                    if called_function_name(instruction).as_deref() == Some(callee.as_str()) {
                        candidates.push((function, instruction, block_name(block), ordinal));
                    }
                    ordinal += 1;
                }
            }
        }
        if candidates.len() != 1 {
            for entry in entries {
                diagnostics.push(IrInstrumentationDiagnostic {
                    code: "STG-IR-W007".to_string(),
                    message: format!("source call {callee} has {} matching LLVM calls in {pou}",
                                     candidates.len()),
                    stable_id: Some(entry.stable_id.clone()),
                    function: Some(pou.clone()),
                });
            }
            continue;
        }
        let (function, instruction, source_block, ordinal) = candidates[0].clone();
        let events = entries.into_iter().map(|entry| MetadataEvent {
            runtime_id: entry.runtime_id,
            stable_id: entry.stable_id.clone(),
            semantic_edge_ids: entry.semantic_edge_ids.clone(),
            kind: serde_json::to_value(&entry.kind).unwrap().as_str().unwrap().to_string(),
            role: None,
            hazard: entry.hazard.as_ref().map(|hazard|
                serde_json::to_value(hazard).unwrap().as_str().unwrap().to_string()),
            violation_predicate: entry.violation_predicate.clone(),
            successor_hint: None,
            modeling: serde_json::to_value(&entry.modeling).unwrap().as_str().unwrap().to_string(),
        }).collect();
        instrument_hazard(context, module, WorkItem {
            function, instruction, source_block, instruction_ordinal: ordinal,
            payload: MetadataPayload {
                schema_version: "semantist.llvm-semantic/1.0.0".to_string(),
                pou, lowered_ast_id: None, events,
            },
        }, hit_function, hazard_function, mappings, diagnostics)?;
    }
    Ok(())
}

fn collect_work<'ctx>(module: &Module<'ctx>, metadata_kind: u32) -> Result<Vec<WorkItem<'ctx>>> {
    let mut work = Vec::new();
    for function in module.get_functions() {
        for block in function.get_basic_blocks() {
            let source_block = block_name(block);
            let mut instruction = block.get_first_instruction();
            let mut ordinal = 0;
            while let Some(current) = instruction {
                instruction = current.get_next_instruction();
                if let Some(metadata) = current.get_metadata(metadata_kind) {
                    let values = metadata.get_node_values();
                    let payload = values
                        .first()
                        .and_then(|value| match value {
                            BasicMetadataValueEnum::MetadataValue(value) => {
                                value.get_string_value()
                            }
                            _ => None,
                        })
                        .and_then(|value| value.to_str().ok())
                        .ok_or_else(|| anyhow::anyhow!("malformed semantist.semantic metadata"))?;
                    work.push(WorkItem {
                        function,
                        instruction: current,
                        source_block: source_block.clone(),
                        instruction_ordinal: ordinal,
                        payload: serde_json::from_str(payload)?,
                    });
                }
                ordinal += 1;
            }
        }
    }
    Ok(work)
}

fn instrument_successors<'ctx>(
    context: &'ctx Context,
    item: WorkItem<'ctx>,
    successor_count: u32,
    hit_function: FunctionValue<'ctx>,
    mappings: &mut Vec<IrSemanticMapping>,
    diagnostics: &mut Vec<IrInstrumentationDiagnostic>,
) -> Result<()> {
    let mut by_successor = BTreeMap::<u32, Vec<MetadataEvent>>::new();
    for event in item.payload.events.clone() {
        let successor = event.successor_hint.or((successor_count == 1).then_some(0));
        let Some(successor) = successor else {
            diagnostics.push(IrInstrumentationDiagnostic {
                code: "STG-IR-E004".to_string(),
                message: "CFG-edge event has no successor index".to_string(),
                stable_id: Some(event.stable_id),
                function: Some(function_name(item.function)),
            });
            continue;
        };
        if successor >= successor_count {
            diagnostics.push(IrInstrumentationDiagnostic {
                code: "STG-IR-E005".to_string(),
                message: format!(
                    "successor index {successor} exceeds terminator successor count {successor_count}"
                ),
                stable_id: Some(event.stable_id),
                function: Some(function_name(item.function)),
            });
            continue;
        }
        by_successor.entry(successor).or_default().push(event);
    }

    for (successor_index, mut events) in by_successor {
        events.sort_by_key(|event| event.runtime_id);
        let destination_ref =
            unsafe { LLVMGetSuccessor(item.instruction.as_value_ref(), successor_index) };
        let destination = unsafe { inkwell::basic_block::BasicBlock::new(destination_ref) }
            .ok_or_else(|| anyhow::anyhow!("LLVM successor is not a valid basic block"))?;
        let probe_name = format!(
            "stg.probe.{}",
            events
                .iter()
                .map(|event| event.runtime_id.to_string())
                .collect::<Vec<_>>()
                .join(".")
        );
        let probe = context.append_basic_block(item.function, &probe_name);
        let builder = context.create_builder();
        builder.position_at_end(probe);
        for event in &events {
            builder.build_call(
                hit_function,
                &[context
                    .i32_type()
                    .const_int(event.runtime_id as u64, false)
                    .into()],
                "",
            )?;
        }
        builder.build_unconditional_branch(destination)?;
        unsafe {
            LLVMSetSuccessor(
                item.instruction.as_value_ref(),
                successor_index,
                probe.as_mut_ptr(),
            )
        };
        for event in events {
            mappings.push(mapping(
                &item,
                &event,
                Some(successor_index),
                Some(block_name(destination)),
                Some(block_name(probe)),
                IrMappingKind::CfgEdge,
            ));
        }
    }
    Ok(())
}

fn instrument_terminal<'ctx>(
    context: &'ctx Context,
    item: WorkItem<'ctx>,
    hit_function: FunctionValue<'ctx>,
    mappings: &mut Vec<IrSemanticMapping>,
) -> Result<()> {
    let builder = context.create_builder();
    builder.position_before(&item.instruction);
    for event in &item.payload.events {
        builder.build_call(
            hit_function,
            &[context
                .i32_type()
                .const_int(event.runtime_id as u64, false)
                .into()],
            "",
        )?;
        mappings.push(mapping(
            &item,
            event,
            None,
            None,
            None,
            IrMappingKind::Terminal,
        ));
    }
    Ok(())
}

fn instrument_hazard<'ctx>(
    context: &'ctx Context,
    module: &Module<'ctx>,
    item: WorkItem<'ctx>,
    hit_function: FunctionValue<'ctx>,
    hazard_function: FunctionValue<'ctx>,
    mappings: &mut Vec<IrSemanticMapping>,
    diagnostics: &mut Vec<IrInstrumentationDiagnostic>,
) -> Result<()> {
    let builder = context.create_builder();
    builder.position_before(&item.instruction);
    for event in &item.payload.events {
        match event.kind.as_str() {
            "hazard_reach" | "external_output" => {
                builder.build_call(
                    hit_function,
                    &[context
                        .i32_type()
                        .const_int(event.runtime_id as u64, false)
                        .into()],
                    "",
                )?;
                mappings.push(mapping(
                    &item,
                    event,
                    None,
                    None,
                    None,
                    IrMappingKind::HazardReach,
                ));
            }
            "hazard_violation" => {
                if let Some(condition) =
                    hazard_condition(context, &builder, module, item.instruction, event)?
                {
                    builder.build_call(
                        hazard_function,
                        &[
                            context
                                .i32_type()
                                .const_int(event.runtime_id as u64, false)
                                .into(),
                            condition.into(),
                        ],
                        "",
                    )?;
                    mappings.push(mapping(
                        &item,
                        event,
                        None,
                        None,
                        None,
                        IrMappingKind::HazardViolation,
                    ));
                } else {
                    diagnostics.push(IrInstrumentationDiagnostic {
                        code: "STG-IR-W006".to_string(),
                        message: format!(
                            "no exact runtime violation predicate for hazard {:?} on LLVM opcode {:?} ({})",
                            event.hazard,
                            item.instruction.get_opcode(),
                            event.modeling
                        ),
                        stable_id: Some(event.stable_id.clone()),
                        function: Some(function_name(item.function)),
                    });
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn hazard_condition<'ctx>(
    context: &'ctx Context,
    builder: &inkwell::builder::Builder<'ctx>,
    module: &Module<'ctx>,
    instruction: InstructionValue<'ctx>,
    event: &MetadataEvent,
) -> Result<Option<inkwell::values::IntValue<'ctx>>> {
    let opcode = instruction.get_opcode();
    match (event.hazard.as_deref(), opcode) {
        (
            Some("division" | "modulo"),
            InstructionOpcode::SDiv
            | InstructionOpcode::UDiv
            | InstructionOpcode::SRem
            | InstructionOpcode::URem,
        ) => {
            let dividend = operand_value(instruction, 0)?.into_int_value();
            let divisor = operand_value(instruction, 1)?.into_int_value();
            let zero = divisor.get_type().const_zero();
            let zero_divisor =
                builder.build_int_compare(IntPredicate::EQ, divisor, zero, "stg.div.zero")?;
            if opcode != InstructionOpcode::SDiv {
                return Ok(Some(zero_divisor));
            }
            let width = divisor.get_type().get_bit_width();
            if width == 0 || width > 64 {
                return Ok(Some(zero_divisor));
            }
            let minus_one = divisor.get_type().const_all_ones();
            let min = dividend
                .get_type()
                .const_int(1u64 << (width.saturating_sub(1)), false);
            let divisor_minus_one = builder.build_int_compare(
                IntPredicate::EQ,
                divisor,
                minus_one,
                "stg.div.minus1",
            )?;
            let dividend_min =
                builder.build_int_compare(IntPredicate::EQ, dividend, min, "stg.div.min")?;
            let overflow =
                builder.build_and(divisor_minus_one, dividend_min, "stg.div.overflow")?;
            Ok(Some(builder.build_or(
                zero_divisor,
                overflow,
                "stg.div.violation",
            )?))
        }
        (Some("division" | "modulo"), InstructionOpcode::FDiv | InstructionOpcode::FRem) => {
            let divisor = operand_value(instruction, 1)?.into_float_value();
            Ok(Some(builder.build_float_compare(
                FloatPredicate::OEQ,
                divisor,
                divisor.get_type().const_float(0.0),
                "stg.div.zero",
            )?))
        }
        (Some("pointer_access"), InstructionOpcode::Load) => {
            let pointer = operand_value(instruction, 0)?.into_pointer_value();
            Ok(Some(builder.build_is_null(pointer, "stg.ptr.null")?))
        }
        (Some("pointer_access"), InstructionOpcode::Store) => {
            let pointer = operand_value(instruction, 1)?.into_pointer_value();
            Ok(Some(builder.build_is_null(pointer, "stg.ptr.null")?))
        }
        (Some("arithmetic_boundary"), InstructionOpcode::Add)
        | (Some("arithmetic_boundary"), InstructionOpcode::Sub)
        | (Some("arithmetic_boundary"), InstructionOpcode::Mul) => {
            let Some(ViolationPredicate::IntegerOverflow {
                operator, signed: Some(signed), ..
            }) = event.violation_predicate.as_ref()
            else {
                return Ok(None);
            };
            let expected_opcode = match operator.as_str() {
                "+" => InstructionOpcode::Add,
                "-" => InstructionOpcode::Sub,
                "*" => InstructionOpcode::Mul,
                _ => return Ok(None),
            };
            if opcode != expected_opcode {
                return Ok(None);
            }
            let prefix = if *signed { "s" } else { "u" };
            let operation = match opcode {
                InstructionOpcode::Add => "add",
                InstructionOpcode::Sub => "sub",
                InstructionOpcode::Mul => "mul",
                _ => unreachable!(),
            };
            let left = operand_value(instruction, 0)?.into_int_value();
            let right = operand_value(instruction, 1)?.into_int_value();
            let intrinsic = Intrinsic::find(&format!("llvm.{prefix}{operation}.with.overflow"))
                .ok_or_else(|| anyhow::anyhow!("LLVM overflow intrinsic is unavailable"))?;
            let function = intrinsic
                .get_declaration(module, &[left.get_type().into()])
                .ok_or_else(|| anyhow::anyhow!("cannot declare LLVM overflow intrinsic"))?;
            let result = builder
                .build_call(
                    function,
                    &[left.into(), right.into()],
                    "stg.overflow.checked",
                )?
                .try_as_basic_value()
                .basic()
                .ok_or_else(|| anyhow::anyhow!("overflow intrinsic returned void"))?
                .into_struct_value();
            Ok(Some(
                builder
                    .build_extract_value(result, 1, "stg.arithmetic.overflow")?
                    .into_int_value(),
            ))
        }
        (Some("arithmetic_boundary"), InstructionOpcode::FAdd)
        | (Some("arithmetic_boundary"), InstructionOpcode::FSub)
        | (Some("arithmetic_boundary"), InstructionOpcode::FMul) => {
            let result = instruction.as_any_value_enum().into_float_value();
            position_after(builder, instruction);
            let nan = builder.build_float_compare(
                FloatPredicate::UNO,
                result,
                result,
                "stg.float.nan",
            )?;
            let positive_infinity = builder.build_float_compare(
                FloatPredicate::OEQ,
                result,
                result.get_type().const_float(f64::INFINITY),
                "stg.float.posinf",
            )?;
            let negative_infinity = builder.build_float_compare(
                FloatPredicate::OEQ,
                result,
                result.get_type().const_float(f64::NEG_INFINITY),
                "stg.float.neginf",
            )?;
            let infinity =
                builder.build_or(positive_infinity, negative_infinity, "stg.float.inf")?;
            Ok(Some(builder.build_or(
                nan,
                infinity,
                "stg.float.nonfinite",
            )?))
        }
        (Some("dangerous_conversion"), InstructionOpcode::FPToSI)
        | (Some("dangerous_conversion"), InstructionOpcode::FPToUI) => {
            let Some(ViolationPredicate::FloatToInteger {
                target_bit_width: Some(width), signed: Some(signed), ..
            }) = event.violation_predicate.as_ref() else {
                return Ok(None);
            };
            let target = instruction.as_any_value_enum().into_int_value();
            if *width == 0 || *width > 64 || target.get_type().get_bit_width() != *width
                || *signed != (opcode == InstructionOpcode::FPToSI) {
                return Ok(None);
            }
            let source = operand_value(instruction, 0)?.into_float_value();
            float_to_integer_condition(context, builder, source, *width, *signed).map(Some)
        }
        (Some("dangerous_conversion"), InstructionOpcode::Call) => {
            let Some(ViolationPredicate::FloatToInteger {
                target_bit_width: Some(width), signed: Some(signed), ..
            }) = event.violation_predicate.as_ref() else {
                return Ok(None);
            };
            if *width == 0 || *width > 64 {
                return Ok(None);
            }
            for operand in 0..instruction.get_num_operands() {
                if let BasicValueEnum::FloatValue(source) = operand_value(instruction, operand)? {
                    return float_to_integer_condition(context, builder, source, *width, *signed).map(Some);
                }
            }
            Ok(None)
        }
        (Some("array_access"), InstructionOpcode::Load)
        | (Some("array_access"), InstructionOpcode::Store)
        | (Some("array_access"), InstructionOpcode::GetElementPtr) => {
            let Some(ViolationPredicate::ArrayBounds { dimensions }) =
                event.violation_predicate.as_ref()
            else {
                return Ok(None);
            };
            if dimensions.is_empty() {
                return Ok(None);
            }
            let gep = if opcode == InstructionOpcode::GetElementPtr {
                instruction
            } else {
                let pointer_operand = if opcode == InstructionOpcode::Load {
                    0
                } else {
                    1
                };
                let pointer = operand_value(instruction, pointer_operand)?;
                let Some(gep) = pointer.as_instruction_value() else {
                    return Ok(None);
                };
                gep
            };
            if gep.get_opcode() != InstructionOpcode::GetElementPtr {
                return Ok(None);
            }
            let operand_count = gep.get_num_operands() as usize;
            if operand_count < 2 {
                return Ok(None);
            }
            let indices = if dimensions.len() > 1 && operand_count < dimensions.len() + 2 {
                let flat = operand_value(gep, (operand_count - 1) as u32)?.into_int_value();
                let Some(indices) = flattened_array_indices(flat, dimensions) else {
                    return Ok(None);
                };
                indices
            } else if operand_count >= dimensions.len() + 1 {
                (0..dimensions.len()).map(|index| {
                    operand_value(gep, (operand_count - dimensions.len() + index) as u32)
                        .map(|value| value.into_int_value())
                }).collect::<Result<Vec<_>>>()?
            } else {
                return Ok(None);
            };
            let mut violation = None;
            for (dimension_index, dimension) in dimensions.iter().enumerate() {
                let (Some(lower), Some(upper)) = (dimension.lower, dimension.upper) else {
                    return Ok(None);
                };
                let length = i128::from(upper) - i128::from(lower) + 1;
                if length <= 0 || length > u64::MAX as i128 {
                    return Ok(None);
                }
                let index = indices[dimension_index];
                let zero = index.get_type().const_zero();
                let extent = index.get_type().const_int((length - 1) as u64, false);
                let below = builder.build_int_compare(
                    IntPredicate::SLT, index, zero, "stg.array.below")?;
                let above = builder.build_int_compare(
                    IntPredicate::SGT, index, extent, "stg.array.above")?;
                let outside = builder.build_or(below, above, "stg.array.dimension_oob")?;
                violation = Some(match violation {
                    Some(previous) => builder.build_or(
                        previous, outside, "stg.array.out_of_bounds")?,
                    None => outside,
                });
            }
            Ok(violation)
        }
        (Some("dangerous_conversion"), InstructionOpcode::Trunc) => {
            narrowing_condition(builder, instruction, instruction, event)
        }
        (Some("dangerous_conversion"), InstructionOpcode::Store) => {
            let value = operand_value(instruction, 0)?;
            if value.is_const() {
                return Ok(narrowing_constant_condition(context, event));
            }
            let Some(trunc) = value.as_instruction_value() else {
                return Ok(None);
            };
            if trunc.get_opcode() != InstructionOpcode::Trunc {
                return Ok(None);
            }
            narrowing_condition(builder, instruction, trunc, event)
        }
        _ => Ok(None),
    }
}

fn flattened_array_indices<'ctx>(
    flat: inkwell::values::IntValue<'ctx>,
    dimensions: &[crate::model::ArrayDimension],
) -> Option<Vec<inkwell::values::IntValue<'ctx>>> {
    fn terms<'ctx>(
        value: inkwell::values::IntValue<'ctx>, coefficient: u64,
        output: &mut Vec<(u64, inkwell::values::IntValue<'ctx>)>,
    ) -> Option<()> {
        if let Some(constant) = value.get_zero_extended_constant() {
            return (constant == 0).then_some(());
        }
        if let Some(instruction) = value.as_instruction_value() {
            match instruction.get_opcode() {
                InstructionOpcode::Add => {
                    terms(operand_value(instruction, 0).ok()?.into_int_value(), coefficient, output)?;
                    terms(operand_value(instruction, 1).ok()?.into_int_value(), coefficient, output)?;
                    return Some(());
                }
                InstructionOpcode::Mul => {
                    let left = operand_value(instruction, 0).ok()?.into_int_value();
                    let right = operand_value(instruction, 1).ok()?.into_int_value();
                    if let Some(multiplier) = left.get_zero_extended_constant() {
                        return terms(right, coefficient.checked_mul(multiplier)?, output);
                    }
                    if let Some(multiplier) = right.get_zero_extended_constant() {
                        return terms(left, coefficient.checked_mul(multiplier)?, output);
                    }
                }
                _ => {}
            }
        }
        output.push((coefficient, value));
        Some(())
    }

    let mut strides = vec![1u64; dimensions.len()];
    for position in (0..dimensions.len().saturating_sub(1)).rev() {
        let next = &dimensions[position + 1];
        let length = next.upper?.checked_sub(next.lower?)?.checked_add(1)?;
        strides[position] = strides[position + 1].checked_mul(u64::try_from(length).ok()?)?;
    }
    let mut components = Vec::new();
    terms(flat, 1, &mut components)?;
    let mut indices = vec![None; dimensions.len()];
    for (coefficient, value) in components {
        let position = strides.iter().position(|stride| *stride == coefficient)?;
        if indices[position].replace(value).is_some() {
            return None;
        }
    }
    indices.into_iter().collect()
}

fn float_to_integer_condition<'ctx>(
    context: &'ctx Context,
    builder: &inkwell::builder::Builder<'ctx>,
    source: inkwell::values::FloatValue<'ctx>,
    width: u32,
    signed: bool,
) -> Result<inkwell::values::IntValue<'ctx>> {
    let value = if source.get_type() == context.f32_type() {
        builder.build_float_ext(source, context.f64_type(), "stg.float_to_int.widen")?
    } else {
        source
    };
    let limit = if signed { width - 1 } else { width };
    let upper = 2.0_f64.powi(limit as i32);
    let exact_lower = !signed || limit < 53;
    let lower = if signed { -upper - if exact_lower { 1.0 } else { 0.0 } } else { -1.0 };
    let below = builder.build_float_compare(
        if exact_lower { FloatPredicate::OLE } else { FloatPredicate::OLT },
        value, value.get_type().const_float(lower),
        "stg.float_to_int.below")?;
    let above = builder.build_float_compare(
        FloatPredicate::OGE, value, value.get_type().const_float(upper),
        "stg.float_to_int.above")?;
    let nonfinite = builder.build_float_compare(
        FloatPredicate::UNO, value, value, "stg.float_to_int.nan")?;
    let outside = builder.build_or(below, above, "stg.float_to_int.range")?;
    Ok(builder.build_or(outside, nonfinite, "stg.float_to_int.invalid")?)
}

fn narrowing_condition<'ctx>(
    builder: &inkwell::builder::Builder<'ctx>,
    anchor: InstructionValue<'ctx>,
    trunc: InstructionValue<'ctx>,
    event: &MetadataEvent,
) -> Result<Option<inkwell::values::IntValue<'ctx>>> {
    let source = operand_value(trunc, 0)?.into_int_value();
    let truncated = trunc.as_any_value_enum().into_int_value();
    if anchor == trunc {
        position_after(builder, trunc);
    }
    let target_signed = match event.violation_predicate.as_ref() {
        Some(ViolationPredicate::Narrowing {
            target_signed,
            signed,
            ..
        }) => target_signed.or(*signed).unwrap_or(false),
        _ => false,
    };
    let restored = if target_signed {
        builder.build_int_s_extend(truncated, source.get_type(), "stg.trunc.restore")?
    } else {
        builder.build_int_z_extend(truncated, source.get_type(), "stg.trunc.restore")?
    };
    Ok(Some(builder.build_int_compare(
        IntPredicate::NE,
        source,
        restored,
        "stg.trunc.loss",
    )?))
}

fn narrowing_constant_condition<'ctx>(
    context: &'ctx Context,
    event: &MetadataEvent,
) -> Option<inkwell::values::IntValue<'ctx>> {
    let ViolationPredicate::Narrowing {
        target_bit_width: Some(target_bit_width),
        target_signed,
        signed,
        source_constant: Some(source_constant),
        ..
    } = event.violation_predicate.as_ref()?
    else {
        return None;
    };
    let target_signed = target_signed.or(*signed)?;
    let violation = !integer_constant_fits(source_constant, *target_bit_width, target_signed)?;
    Some(context.bool_type().const_int(violation as u64, false))
}

fn integer_constant_fits(value: &str, bit_width: u32, signed: bool) -> Option<bool> {
    if bit_width == 0 || bit_width > 128 {
        return None;
    }
    if signed {
        let value = value.parse::<i128>().ok()?;
        if bit_width == 128 {
            return Some(true);
        }
        let magnitude = 1_i128.checked_shl(bit_width - 1)?;
        Some(value >= -magnitude && value <= magnitude - 1)
    } else {
        if value.starts_with('-') {
            return Some(false);
        }
        let value = value.parse::<u128>().ok()?;
        let maximum = if bit_width == 128 {
            u128::MAX
        } else {
            1_u128.checked_shl(bit_width)?.saturating_sub(1)
        };
        Some(value <= maximum)
    }
}

fn position_after(builder: &inkwell::builder::Builder<'_>, instruction: InstructionValue<'_>) {
    if let Some(next) = instruction.get_next_instruction() {
        builder.position_before(&next);
    } else if let Some(block) = instruction.get_parent() {
        builder.position_at_end(block);
    }
}

fn operand_value<'ctx>(
    instruction: InstructionValue<'ctx>,
    index: u32,
) -> Result<inkwell::values::BasicValueEnum<'ctx>> {
    match instruction.get_operand(index) {
        Some(Operand::Value(value)) => Ok(value),
        _ => bail!("LLVM instruction has no value operand {index}"),
    }
}

fn mapping(
    item: &WorkItem<'_>,
    event: &MetadataEvent,
    successor_index: Option<u32>,
    destination_block: Option<String>,
    probe_block: Option<String>,
    mapping_kind: IrMappingKind,
) -> IrSemanticMapping {
    IrSemanticMapping {
        stable_id: event.stable_id.clone(),
        runtime_id: event.runtime_id,
        semantic_edge_ids: event.semantic_edge_ids.clone(),
        target_kind: event.kind.clone(),
        role: event.role.clone(),
        hazard: event.hazard.clone(),
        pou: item.payload.pou.clone(),
        lowered_ast_id: item.payload.lowered_ast_id,
        function: function_name(item.function),
        source_block: item.source_block.clone(),
        source_instruction_ordinal: item.instruction_ordinal,
        successor_index,
        destination_block,
        probe_block,
        mapping_kind,
    }
}

fn get_or_add_hit_function<'ctx>(
    context: &'ctx Context,
    module: &Module<'ctx>,
) -> FunctionValue<'ctx> {
    module
        .get_function("__semantist_semantic_hit")
        .unwrap_or_else(|| {
            module.add_function(
                "__semantist_semantic_hit",
                context
                    .void_type()
                    .fn_type(&[context.i32_type().into()], false),
                None,
            )
        })
}

fn get_or_add_hazard_function<'ctx>(
    context: &'ctx Context,
    module: &Module<'ctx>,
) -> FunctionValue<'ctx> {
    module
        .get_function("__semantist_semantic_hazard")
        .unwrap_or_else(|| {
            module.add_function(
                "__semantist_semantic_hazard",
                context.void_type().fn_type(
                    &[context.i32_type().into(), context.bool_type().into()],
                    false,
                ),
                None,
            )
        })
}

fn function_name(function: FunctionValue<'_>) -> String {
    function.get_name().to_string_lossy().into_owned()
}

fn block_name(block: inkwell::basic_block::BasicBlock<'_>) -> String {
    block.get_name().to_string_lossy().into_owned()
}

fn statistics(
    runtime_ids: &RuntimeIdTable,
    mappings: &[IrSemanticMapping],
    mapped: &BTreeSet<u32>,
) -> IrInstrumentationStatistics {
    IrInstrumentationStatistics {
        metadata_instruction_count: mappings
            .iter()
            .map(|mapping| {
                (
                    mapping.function.as_str(),
                    mapping.source_block.as_str(),
                    mapping.source_instruction_ordinal,
                )
            })
            .collect::<BTreeSet<_>>()
            .len(),
        target_count: runtime_ids.entries.len(),
        mapped_target_count: mapped.len(),
        unmapped_target_count: runtime_ids.entries.len().saturating_sub(mapped.len()),
        cfg_edge_mapping_count: mappings
            .iter()
            .filter(|mapping| matches!(mapping.mapping_kind, IrMappingKind::CfgEdge))
            .count(),
        terminal_mapping_count: mappings
            .iter()
            .filter(|mapping| matches!(mapping.mapping_kind, IrMappingKind::Terminal))
            .count(),
        hazard_reach_count: mappings
            .iter()
            .filter(|mapping| matches!(mapping.mapping_kind, IrMappingKind::HazardReach))
            .count(),
        hazard_violation_count: mappings
            .iter()
            .filter(|mapping| matches!(mapping.mapping_kind, IrMappingKind::HazardViolation))
            .count(),
        probe_block_count: mappings
            .iter()
            .filter_map(|mapping| mapping.probe_block.as_deref())
            .collect::<BTreeSet<_>>()
            .len(),
    }
}

fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    std::fs::write(path, serde_json::to_vec_pretty(value)?)
        .with_context(|| format!("cannot write {}", path.display()))
}

fn normalized_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}
