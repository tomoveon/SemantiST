# SemantiST STG 静态语义模型定义

## 1. 文档目的

本文档定义 SemantiST 使用的 STG（State-aware Semantic Branch Graph，状态感知语义分支图）静态语义模型。

STG 面向 IEC 61131-3 Structured Text 程序，用于为后续语义覆盖、调度、变异、LLVM IR 插桩、约束求解和多周期规划提供统一的编译器语义表示。

当前模型 Schema 为：

```text
semantist.stg/1.2.0
```

STG 只保存静态语义事实和静态特征，不保存动态覆盖次数、模糊测试历史、求解历史或调度权重。

## 2. 编译器语义来源

STG 的正式语义事实来自 RuSTy 编译器内部表示：

```text
ST Source
  -> Source AST
  -> Semantic Index
  -> Type Annotation
  -> Typed/Lowered AST
  -> STG
```

各语义来源的职责如下：

| 语义来源 | 主要职责 |
| --- | --- |
| Source AST | 保留 IF、ELSIF、CASE、WHILE、REPEAT、FOR、EXIT、RETURN 等源码级结构 |
| Semantic Index | 解析符号、限定名、声明、POU、类型声明和调用目标 |
| Type Annotation | 提供表达式实际类型、期望类型、转换和调用解析结果 |
| Typed/Lowered AST | 提供经过 RuSTy 官方 lowering 后、接近 Codegen 的结构和节点位置 |

STG 不以正则表达式、源码字符串扫描、Tree-sitter 或 `--ast-lowered` Debug 文本作为正式语义来源。

这些方法可以作为兼容、展示或对照手段，但不能替代 RuSTy 编译器语义。

## 3. 项目级模型

一个项目的完整模型定义为：

```text
M = <Metadata, {M_P}, Diagnostics, Statistics>
```

其中：

- `Metadata` 是构建和输入元数据。
- `{M_P}` 是项目中各个 POU 的语义模型集合。
- `Diagnostics` 是项目级诊断。
- `Statistics` 是项目级统计信息。

项目元数据至少包括：

- STG Schema 版本。
- SemantiST 生成器名称和版本。
- RuSTy 版本。
- RuSTy Git Revision。
- 项目根目录。
- 输入文件的项目相对路径。
- 输入文件 SHA-256。
- 是否采用确定性构建。
- 本模型使用的编译器语义来源。

## 4. POU 语义模型

对每个 POU，定义：

```text
M_P = <G_P, Sigma_P, mu_P, T_P>
```

也可写成：

```text
M_P = <G_P, Σ_P, μ_P, T_P>
```

其中：

- `P` 表示一个 Program Organization Unit。
- `G_P` 是该 POU 的状态感知语义分支图。
- `Σ_P` 是该 POU 的辅助语义环境。
- `μ_P` 是 Source AST、Lowered AST、STG 和未来 LLVM IR 位置之间的映射。
- `T_P` 是可供插桩、覆盖、调度、变异和规划使用的语义目标集合。

支持描述的 POU 类型包括：

- FUNCTION
- FUNCTION_BLOCK
- PROGRAM
- METHOD
- ACTION
- CLASS
- 其他 RuSTy 可识别 POU

每个 POU 描述符保存：

- POU 名称。
- POU 限定名。
- POU 类型。
- POU 源码范围。
- 是否具有持久状态。
- POU 模型稳定 ID。

## 5. 状态感知语义分支图 G_P

定义：

```text
G_P = <V_P, E_P, Entry_P, Exit_P, CycleEntry_P, CycleExit_P>
```

其中：

- `V_P` 是语义节点集合。
- `E_P` 是语义边集合。
- `Entry_P` 是 POU 调用入口。
- `Exit_P` 是 POU 调用出口。
- `CycleEntry_P` 是有状态 POU 的扫描周期入口。
- `CycleExit_P` 是有状态 POU 的扫描周期出口。

FUNCTION 通常只有 Entry 和 Exit。

FUNCTION_BLOCK 等有状态 POU 同时具有 CycleEntry 和 CycleExit。

### 5.1 结构节点

结构节点包括：

| 节点 | 含义 |
| --- | --- |
| Entry | POU 调用入口 |
| Exit | POU 调用出口 |
| CycleEntry | Function Block 当前扫描周期入口 |
| CycleExit | Function Block 当前扫描周期出口 |
| LoopLatch | 循环体完成后准备返回谓词的位置 |
| LoopControl | EXIT、CONTINUE 等显式循环控制位置 |

### 5.2 谓词节点

谓词节点表示一次具有语义角色的条件求值。

支持的谓词类型包括：

- IF
- ELSIF
- CASE
- WHILE
- REPEAT
- FOR

谓词节点保存：

- 节点稳定 ID。
- 谓词结构类型。
- 源码位置。
- Source AstId。
- Lowered AstId。
- 条件复杂度。
- 嵌套深度。
- 输入依赖数量。
- 状态依赖数量。
- 所属循环。
- 调用复杂度。

谓词和谓词结果必须是不同节点。

例如：

```text
IF Predicate
  -> True Outcome
  -> False Outcome
```

不能只建立一个 IF 节点并把 True、False 隐藏在边属性中。

### 5.3 Outcome 节点

Outcome 是一等语义节点，也是一等语义目标。

支持的 Outcome Role 包括：

| Outcome Role | 含义 |
| --- | --- |
| True | 条件结果为真 |
| False | 条件结果为假 |
| Else | 进入显式 ELSE 结构 |
| CaseLabel | 命中某个 CASE 标签或范围标签 |
| CaseDefault | 命中 CASE Default |
| LoopEnter | 条件允许进入循环体 |
| LoopContinue | REPEAT 等结构继续下一轮 |
| LoopExit | 条件导致循环退出 |
| LoopBack | 循环体完成后的结构回边 |
| ExplicitExit | 执行显式 EXIT |
| ExplicitContinue | 执行显式 CONTINUE |
| Return | 执行显式 RETURN |
| CycleEntry | 进入一次扫描周期 |
| CycleExit | 退出一次扫描周期 |

每个 Outcome 保存：

- 所属谓词节点。
- 语义角色。
- Outcome 序号。
- 实际语义值。

Outcome Value 支持：

- Boolean
- Expression
- Default
- Structural

Outcome Role 和 Outcome Value 必须分开保存。

例如 REPEAT：

```st
REPEAT
    body;
UNTIL condition
END_REPEAT;
```

其源码语义是：

```text
condition = False -> LoopContinue
condition = True  -> LoopExit
```

不能仅根据布尔值把 True 解释成普通分支进入。

### 5.4 CASE 语义

CASE 的每个标签、范围标签和 Default 都是独立 Outcome。

例如：

```st
CASE x OF
    1: ...
    2..4: ...
ELSE
    ...
END_CASE;
```

至少产生：

```text
CaseLabel(1)
CaseLabel(2..4)
CaseDefault
```

即使多个标签经过 lowering 后跳向同一个 LLVM Basic Block，也必须保留不同的 STG 语义身份。

### 5.5 危险节点

危险节点表示可能需要独立调度和插桩的危险操作。

支持的危险类型包括：

| Hazard Kind | 含义 |
| --- | --- |
| Division | 除法相关危险 |
| Modulo | 取模相关危险 |
| ArrayAccess | 数组索引或边界危险 |
| PointerAccess | 指针访问危险 |
| DangerousConversion | 潜在信息丢失或危险转换 |
| ArithmeticBoundary | 有限位宽算术边界 |
| ExternalOutput | 外部可见输出 |
| UserProperty | 用户定义安全性质 |

危险节点保存：

- Hazard Kind。
- 对应表达式 ID。
- 建模状态。
- 危险语义说明。
- 源码位置。
- Source AST 和 Lowered AST 来源。

一个危险操作通常对应两个目标：

```text
HazardReach
HazardViolation
```

其中：

- HazardReach 表示执行到危险操作。
- HazardViolation 表示危险条件实际成立。

执行到除法、GEP、load 或 store 不能直接解释为发生违规。

### 5.6 PropertyViolation 节点

PropertyViolation 用于表示用户定义安全性质被违反。

模型已经保留：

- PropertyViolation 节点类型。
- PropertyViolation 目标类型。
- UserProperty Hazard Kind。
- Property 变量角色。

当前尚未定义完整的用户性质描述语言前端，因此该部分属于保留接口。

## 6. 语义边 E_P

图中定义三类边：

```text
E_P = E_eval union E_ctrl union E_temp
```

分别为：

- Evaluation Edge
- Control-transfer Edge
- Temporal Edge

### 6.1 Evaluation Edge

Evaluation Edge 从谓词节点连接到 Outcome 节点：

```text
Predicate -> Outcome
```

Evaluation Edge 保存：

- Edge Stable ID。
- 起点节点。
- 终点 Outcome。
- 结构化 Guard Expression ID。
- 是否为回边。

Guard 必须引用 `Σ_P` 中的 Typed Expression DAG，不能只保存源码字符串。

互补条件应显式表示。

例如：

```text
IF True Edge  Guard = condition
IF False Edge Guard = NOT(condition)
```

CASE Guard 可以表示：

- 等值标签。
- 标签集合。
- 范围条件。
- Default 条件。

### 6.2 Control-transfer Edge

Control-transfer Edge 从一个语义节点连接到下一个语义节点，并概括两者之间的直线代码：

```text
Outcome -> Next Semantic Node
```

Control-transfer Edge 保存 Transfer Summary。

定义：

```text
TransferSummary =
  <Modeling, Operations, Reads, Writes,
   VersionsIn, VersionsOut, Hold>
```

其中：

- `Modeling` 是摘要建模状态。
- `Operations` 是按 ST 执行顺序排列的操作。
- `Reads` 是读取符号集合。
- `Writes` 是写入符号集合。
- `VersionsIn` 是进入边时的局部版本。
- `VersionsOut` 是离开边时的局部版本。
- `Hold` 描述未修改变量的保持语义。

当前保持语义为：

```text
Implicit Hold
```

即未出现在 Writes 中的变量保持原值。

### 6.3 Transfer Operation

Transfer Operation 支持：

#### Assignment

保存：

- 目标表达式。
- 目标限定符号。
- 右值表达式。
- 读取符号集合。
- 建模状态。
- 赋值前版本。
- 赋值后版本。
- 类型转换。
- 源码位置。

示意：

```text
x#1 := y#0 + 1
```

#### Call

保存：

- Callee。
- 参数表达式。
- 调用副作用。
- 源码位置。

调用副作用包括：

- 读取集合。
- 写入集合。
- 是否外部调用。
- 建模状态。

#### Return

表示显式 RETURN。

FUNCTION 中的 RETURN 终止当前调用并连接到 Exit。

FUNCTION_BLOCK 中的 RETURN 结束当前扫描周期并连接到 CycleExit。

#### Allocation

表示编译器语义中可识别的分配或临时对象建立。

保存对象名称、类型和源码位置。

#### Opaque

表示当前无法安全解释的操作。

保存：

- AST Kind。
- 无法建模的原因。
- 可用的源码位置。

Opaque 不允许被伪装成 Exact。

### 6.4 Type Conversion

类型转换保存：

- 源类型。
- 目标类型。
- 建模状态。
- 是否潜在窄化。

例如：

```text
DINT(32, signed) -> INT(16, signed)
```

属于潜在窄化转换。

### 6.5 Temporal Edge

Temporal Edge 用于表示有状态 POU 相邻扫描周期之间的状态继承：

```text
CycleExit(n) -> CycleEntry(n + 1)
```

Temporal Summary 保存：

- 建模状态。
- 持久状态符号集合。
- 状态继承和更新规则。

Temporal Edge 是静态时间语义关系，不要求在单次 LLVM 函数调用中存在对应的物理 CFG Edge。

## 7. 辅助语义环境 Σ_P

定义：

```text
Σ_P =
  <Symbols, Types, Expressions, DefUse,
   TargetDependencies, Calls, PersistentState>
```

### 7.1 Symbols

每个符号保存：

- Symbol Stable ID。
- 名称。
- 限定名。
- IEC 类型名称。
- Variable Role。
- 是否为常量。
- 配置来源。
- 是否 RETAIN。
- 是否按引用传递。
- 声明源码位置。
- 初始值。
- 已知布局偏移。

Variable Role 包括：

- Input
- Output
- InOut
- Local
- Temporary
- PersistentState
- Return
- Global
- External
- Property

配置来源用于区分：

- IEC Constant。
- `VAR_INPUT {constant}` 等供应商或项目约定。

### 7.2 IEC Types

类型信息保存：

- 类型名称。
- Type Kind。
- 存储位宽。
- IEC 语义位宽。
- 有符号性。
- 元素类型。
- 数组维度。
- 结构体字段。
- 字符串编码。
- 字符串容量。
- 指针目标类型。
- 是否为 type-safe pointer。
- 类型声明源码位置。
- 建模状态。

Type Kind 包括：

- Integer
- Float
- Boolean
- Array
- String
- Struct
- Pointer
- Enum
- Subrange
- Alias
- Interface
- Void
- Generic

数组维度保存：

```text
lower bound
upper bound
```

结构体字段保存：

- 字段名称。
- 字段限定名。
- 字段类型。
- 已知 bit offset。

若 RuSTy Index 未提供完整 ABI Padding 信息，STG 不得假定字段紧密排列。

### 7.3 Typed Expression DAG

所有条件、右值、索引和危险操作数都使用结构化 Typed Expression DAG 表达。

每个表达式保存：

- Expression Stable ID。
- Expression Kind。
- 实际类型。
- Type Hint。
- 建模状态。
- Operand Expression IDs。
- 读取符号集合。
- 源码位置。

Expression Kind 包括：

- Literal
- Variable
- Binary
- Unary
- Call
- ArrayIndex
- MemberAccess
- PointerDeref
- AddressOf
- Cast
- DirectAccess
- HardwareAccess
- Range
- List
- CaseCondition
- Opaque

DirectAccess 用于表示：

```st
IN.0
WORD_VALUE.%X3
```

等位级直接访问。

Literal Value 支持：

- Null
- Integer
- Real
- Boolean
- String
- WideString
- Duration
- Date
- TimeOfDay
- DateTime
- Array

### 7.4 Def-Use

Def-Use Link 表示符号定义和使用之间的关系。

每条关系保存：

- 被定义符号。
- 被使用符号。
- 定义发生的 Control-transfer Edge。
- 使用发生的 Expression。

### 7.5 Target Dependencies

Target Dependencies 保存从语义目标反向追踪得到的依赖符号。

例如：

```text
IF True Target
  <- condition x > state.limit
  <- Input x
  <- PersistentState state.limit
```

该信息供后续语义调度、定向变异和约束求解使用。

### 7.6 Call Summary

调用摘要保存：

- Callee。
- 调用类型。
- 建模状态。
- Reads。
- Writes。
- 是否外部调用。

当前内部调用不会自动进行完整跨过程函数体内联。

简单内部调用可以根据已知签名和副作用进行摘要。

无法精确解释的库调用使用 Conservative 或 Opaque 摘要。

### 7.7 Persistent State

有状态 POU 的 Persistent State 保存：

- 持久状态结构类型。
- 持久符号集合。
- Cycle Inputs。
- InOut Symbols。
- Configuration Symbols。
- 初始化映射。

普通输入、引用参数、配置输入和跨周期状态必须分开表示。

## 8. 跨层映射 μ_P

定义：

```text
μ_P =
  <NodeMappings, ExpressionMappings, FutureIrLocations>
```

映射链为：

```text
Source Location
  <-> Source AST
  <-> STG Node / Expression / Target
  <-> Typed/Lowered AST
  <-> LLVM IR Location
```

### 8.1 Node Mapping

每个 Node Mapping 保存：

- Semantic Node ID。
- POU 限定名。
- 源码范围。
- Source AstIds。
- Lowered AstIds。

### 8.2 Expression Mapping

每个 Expression Mapping 保存：

- Expression ID。
- 源码范围。
- Source AstIds。
- Lowered AstIds。

### 8.3 IR Location

IR Location 支持保存：

- LLVM Module。
- LLVM Function。
- Basic Block。
- Instruction Ordinal。

映射允许：

- 一对一。
- 一对多。
- 多对一。

一个源码语义目标可能经过 lowering 后对应多个 Lowered AST 节点或多个 LLVM CFG Edge。

多个源码语义目标也可能共享同一条 LLVM CFG Edge，例如 IF False 和显式 ELSE Entry。

### 8.4 AstId 与 Stable ID

RuSTy AstId 是单次编译中的临时标识。

AstId 可以用于当前构建中的跨层锚定，但不能作为持久语义 ID。

稳定 ID 应基于：

```text
Project-relative Source Path
+ POU Qualified Name
+ Source Range
+ Semantic Structure Kind
+ Outcome Role
+ Deterministic Ordinal
```

同一源码和配置重复构建时，Stable ID 应保持稳定。

## 9. 语义目标集合 T_P

定义：

```text
T_P = {t_1, t_2, ..., t_n}
```

每个 Semantic Target 保存：

- Stable Target ID。
- 所属 STG Node ID。
- 相关 Edge IDs。
- Target Kind。
- Outcome Role。
- 源码位置。
- Backward Dependencies。
- Modeling Status。

Target Kind 包括：

| Target Kind | 含义 |
| --- | --- |
| BranchOutcome | IF、ELSIF、ELSE 等分支结果 |
| LoopOutcome | LoopEnter、LoopContinue、LoopExit、LoopBack |
| CaseOutcome | CASE 标签、范围标签和 Default |
| ControlTransfer | 需要独立观察的控制转移 |
| Cycle | CycleEntry 或 CycleExit |
| HazardReach | 执行到危险操作 |
| HazardViolation | 危险条件实际成立 |
| ExternalOutput | 执行到外部输出 |
| PropertyViolation | 用户安全性质被违反 |

目标集合是后续系统的统一入口。

同一个目标可以用于：

- LLVM IR 插桩。
- 运行时语义覆盖。
- 语义调度。
- 定向变异。
- 约束求解。
- 多周期规划。
- 报告和可视化。

## 10. 建模状态

所有无法天然保证精确的语义都必须标记 Modeling Status。

### 10.1 Exact

表示 Source AST、Index、Type Annotation 或 Lowered AST 足以精确确定语义。

例如：

- 已解析变量引用。
- 精确 IEC 标量类型。
- IF 条件。
- CASE 标签。
- 显式赋值。
- 显式类型转换。
- 循环拓扑。

### 10.2 Conservative

表示模型能够安全描述语义边界，但不能表达全部细节。

例如：

- 已知参数和副作用、但未内联函数体的调用。
- 非常量隐式窄化在 Lowering 后同时缺少原始宽值和精确结构化常量。
- 缺少完整对象 provenance 的指针运算。

Conservative 不应产生虚假精确结论。

### 10.3 Opaque

表示当前无法安全解释内部语义。

Opaque 至少保存：

- 操作或 AST 类型。
- 无法解释的原因。
- 可获得的 Read/Write Effects。
- 源码位置。

Opaque 不能通过源码字符串猜测升级为 Exact。

## 11. 静态调度特征

每个语义节点可以保存以下静态特征：

- Nesting Depth。
- Condition Complexity。
- Input Dependency Count。
- State Dependency Count。
- Loop ID。
- Call Complexity。

这些特征用于后续调度算法，但 STG 本身不计算最终调度权重。

## 12. 不属于静态 STG 的信息

以下信息不得写入静态 STG：

- 动态覆盖 Bitmap。
- Target Hit Count。
- 路径执行频率。
- 模糊测试命中历史。
- 定向变异或预算失败历史。
- Mutation History。
- 最终调度权重。
- Corpus Energy。
- 动态路径优先级。

这些信息应由运行时反馈层、调度器或 Fuzzer State 单独维护。

## 13. 模型验证约束

一个合法 STG 至少满足以下约束。

### 13.1 引用完整性

- 所有 Node ID 唯一。
- 所有 Edge ID 唯一。
- 所有 Target ID 唯一。
- 所有 Expression ID 唯一。
- Edge 的起点和终点必须存在。
- Guard 引用的表达式必须存在。
- Target 引用的节点和边必须存在。
- Mapping 引用的语义对象必须存在。

### 13.2 谓词完整性

- IF 和 ELSIF 必须具有 True 和 False。
- Outcome 必须属于正确谓词。
- Outcome Ordinal 必须确定。
- 互补布尔 Outcome 必须完整。

### 13.3 CASE 完整性

- 每个 CASE 标签具有独立 Outcome。
- 每个范围标签具有独立语义身份。
- CASE 必须具有 Default 语义。
- 没有显式 ELSE 时也要表示未命中标签的 Default 路径。

### 13.4 循环完整性

- WHILE、REPEAT、FOR 必须具有进入和退出语义。
- 循环必须保留真实回边。
- LoopBack 与条件 Outcome 必须分开。
- REPEAT True 必须解释为 LoopExit。
- 显式 EXIT 必须连接到正确循环出口。

### 13.5 Function Block 完整性

- 有状态 Function Block 必须具有 CycleEntry。
- 有状态 Function Block 必须具有 CycleExit。
- 必须具有 CycleExit 到下一 CycleEntry 的 Temporal Edge。
- 必须保存 Persistent State。
- RETURN 必须结束当前周期。

### 13.6 Transfer Summary 合法性

- 每条 Control-transfer Edge 必须具有合法摘要。
- 无法建模时必须明确为 Opaque。
- 操作顺序必须与 ST 执行顺序一致。
- Reads 和 Writes 必须与操作一致。
- Version Before/After 必须合法。
- 类型转换必须满足类型一致性。

### 13.7 Target 合法性

- 每个目标必须具有 Stable ID。
- 每个目标必须映射到有效节点或边。
- 需要源码映射的目标必须保存源码位置。
- HazardReach 和 HazardViolation 不得混淆。
- 不允许静默遗漏未映射目标。

## 14. 确定性要求

对相同输入、项目根目录、RuSTy Revision 和 SemantiST 配置：

- Stable Node ID 必须一致。
- Stable Edge ID 必须一致。
- Stable Expression ID 必须一致。
- Stable Target ID 必须一致。
- JSON 数组和映射输出顺序必须确定。
- DOT 输出必须确定。
- Model SHA-256 必须一致。

Dense Runtime ID 不属于 STG Stable ID，但构建阶段也应通过 Stable ID 排序确定性分配。

## 15. 输出产物

STG 生成流程输出：

| 产物 | 内容 |
| --- | --- |
| `stg-model.json` | 完整项目语义模型 |
| `stg-manifest.json` | 产物路径和模型摘要 |
| `stg-codegen-map.json` | STG 到 Lowered AST Codegen Anchor |
| `stg-runtime-ids.json` | Stable ID 到 Dense Runtime ID |

`--debug-sidecars` 或 pipeline 的 `--semantic-debug-artifacts` 会额外写
`stg-diagnostics.json`、`stg-statistics.json` 和可选 `dot/*.dot`。主线运行
默认依赖 `stg-model.json` 内嵌的诊断/统计，避免重复 JSON 产物。

完成 LLVM IR 插桩后还会输出：

| 产物 | 内容 |
| --- | --- |
| `stg-ir-mapping.json` | STG Target 到 LLVM 指令和 CFG Edge 的映射 |
| Instrumented LLVM IR | 包含语义探针的最终 LLVM IR |

`stg-ir-diagnostics.json` 仅在显式传入 diagnostics 路径或启用 debug
artifacts 时单独写出；默认诊断已经内嵌在 `stg-ir-mapping.json`。

## 16. 当前精确支持范围

当前可以精确或主要精确建模：

- IEC 标量类型。
- 数组、字符串、结构体和指针类型事实。
- 已解析符号和变量角色。
- IF、ELSIF、CASE、WHILE、REPEAT、FOR。
- CASE 标签、范围标签和 Default。
- EXIT、CONTINUE、RETURN。
- Typed Expression DAG。
- Assignment 和显式转换。
- Control-transfer Summary。
- Read/Write 和局部版本。
- Loop Back Edge。
- Function Block Persistent State。
- Function Block Temporal Edge。
- 除法和取模危险。
- 数组和指针访问危险框架。
- 有限位宽算术危险。
- 危险类型转换。
- External Output Target。

## 17. 当前保守或 Opaque 边界

当前存在以下边界：

- 内部调用不执行通用跨过程函数体内联。
- 部分库调用只能根据签名和参数效果保守摘要。
- 不支持的外部调用为 Opaque。
- 整数字面量隐式窄化即使被 RuSTy 在 LLVM IR 前常量折叠，也会在
  Narrowing Predicate 中保留源常量、源/目标位宽和源/目标 signedness，
  因而可以精确插桩。动态窄化在保留 LLVM trunc 时同样可以精确判断。
- 非常量隐式窄化只有在 Lowering 后既缺少原始宽值、又缺少精确结构化
  常量时才退化为 Conservative。
- Pointer Provenance 和对象生命周期无法仅凭 GEP 完整恢复。
- Index 提供语义字段顺序和类型大小时，不代表已经获得完整 ABI Padding。
- 用户性质模型已经预留，但尚无正式 Property Language Frontend。

当信息不足时，模型必须保守降级，不得从源码字符串或 LLVM 指令形状中猜测出虚假精确语义。

## 18. 总结

`M_P = <G_P, Σ_P, μ_P, T_P>` 将一个 POU 的语义分为四个互补部分：

```text
G_P:
  控制结构、Outcome、循环、危险节点和周期关系

Σ_P:
  符号、类型、表达式、数据依赖、调用和持久状态

μ_P:
  Source AST、Lowered AST、STG 和 LLVM IR 的跨层映射

T_P:
  可插桩、可覆盖、可调度和可规划的稳定语义目标
```

该模型的核心原则是：

```text
高级语义由 RuSTy 编译器语义确定；
实际执行位置由 LLVM IR 确定；
无法精确建模的内容必须明确降级；
所有目标和映射必须稳定、可验证、可追踪。
```
