# SemantiST 工具完整技术说明

> 文档性质：面向后续论文撰写的工具事实底稿，而非论文本身
>
> 对应实现：SemantiST 0.1.0，STG schema `semantist.stg/1.2.0`；源码核对日期：2026-10-09
>
> 事实依据：以当前仓库源码、构建脚本和测试为准；仓库内其他设计文档仅用于定位问题，不作为高于源码的依据
>
> 不含内容：本文不汇报覆盖率、漏洞数、测得运行时间或显著性检验等实验结果；文中的目标数量与预算是当前实验配置

> 在线行为以 Rust 实现为准；证据隔离、周期遥测、预算归因和类型边界见 [算法说明](stg_semantic_ranking_zh.md)。[2026-10-08 算法验收记录](algorithm_validation_20261008_zh.md) 是带源码和样本范围的历史验证证据，不代表此后所有工作区改动或正式实验均已验证。

## 1. 工具定位

SemantiST 是一个面向 IEC 61131-3 Structured Text（ST）程序的语义引导灰盒模糊测试工具。它测试的直接对象不是 ST 源文本解析器，而是由 ST 编译得到的本地可执行目标。工具围绕某个选定的 Program Organization Unit（POU）建立测试目标，当前自动 Harness 支持 `FUNCTION` 和 `FUNCTION_BLOCK`；STG 前端还能够表示 `PROGRAM`、`METHOD`、`ACTION` 和 `CLASS`，但这不等同于这些 POU 都已经具备端到端 Harness 支持。

工具同时保留两类信息：

- 传统执行信息：AFL 风格边覆盖、执行时间以及崩溃、超时、OOM、ASan/UBSan 等运行时结果；
- 编译器语义信息：分支结果、循环结果、控制转移、扫描周期边界、危险操作的可达与违反目标，以及与这些目标相关的表达式、数据依赖、状态和类型。

SemantiST 的关键设计不是用静态分析替代动态执行，也不是仅给基本块重新命名，而是建立一条跨层映射链：

```text
ST 源程序与项目配置
  ├─ 源级 RuSTy AST ──► Semantic Target Graph（STG）
  │                         ├─ 稳定目标 ID
  │                         ├─ 控制/数据/状态/危险语义
  │                         └─ 语义任务与初始种子
  │
  └─ 经 lowering 的 RuSTy AST ──► 带语义元数据的 LLVM IR
                                    └─ 语义探针 + AFL 探针 + Sanitizer
                                                    │
结构化 testcase ──► C Harness ──► 目标执行 ──► 双覆盖反馈/状态反馈/运行时结果
                                                    │
                   任务调度、类型感知变异、发现记录、回放、最小化和报告
```

这条链路中的稳定 ID 负责把源级目标、LLVM 探针、运行时 bitmap、任务状态和报告证据连接起来。动态执行仍然是判定目标是否命中以及是否发生运行时故障的依据。

## 2. 实现组成与技术栈

仓库是 Python 与 Rust 的混合工程：

- Python 3.11 及以上：顶层流水线、ST 声明解析、Harness 生成、方言归一化、兼容性清单、语义任务规划、发现管理、最小化和报告；
- Rust 2021：STG 提取与 LLVM IR 插桩器、基于 LibAFL 0.15.4 的 fuzzing engine；
- RuSTy：ST 解析、索引、类型语义、lowering 和 LLVM 代码生成。STG crate 在源码中固定记录 RuSTy 版本 `0.5.0` 和提交 `be1de6f175ec1bed7928904dfb694208a24043fb`；
- LLVM/Clang 21：IR 规范化、内部化、边界检查、目标编译和链接；
- AFL++：目标端的 `trace-pc-guard` 覆盖运行时；
- ASan 与 UBSan：通用单目标构建默认的内存安全和未定义行为检测器；当前比较实验的 SemantiST 适配器明确选择 ASan，详见第 11 节。

Python 包版本为 0.1.0。主要命令入口为：

| 命令 | 作用 |
| --- | --- |
| `semantist` | 完整构建、任务生成、fuzz 和报告流水线 |
| `semantist-compile` | 编译 ST 项目并可执行语义 IR 插桩 |
| `semantist-prepare-ir` | 对目标 LLVM IR 做兼容化、内部化和边界检查准备 |
| `semantist-harness` | 从 IR 和 STG 模型生成 C Harness 和基础种子，或校验固定 Harness |
| `semantist-report` | 从一次运行目录回放证据并生成报告 |
| `semantist-minimize` | 对结构化复现输入做确定性最小化 |
| `semantist-check-llm` | 检查可选的报告 LLM 服务配置 |
| `python run_experiment.py` | 在仓库根目录运行 `main`/`smoke` 实验预设，支持工具、目标、预算和 CPU 覆盖 |

Dockerfile 指定 Ubuntu 24.04、默认 Rust 1.90.0、LLVM 21、打补丁且固定 revision 的 RuSTy，并预编译 IEC 标准库和 release binaries。AFL++ 等系统包由 apt 安装；Ubuntu 基础镜像和所有 apt 包并未锁定到 digest/精确包版本，所以重新构建不等于位级相同的环境，正式实验还应保存实际镜像 ID 和环境记录。主仓库使用 MIT 许可证；外部 benchmark 或库仍需分别遵守其许可证。

## 3. 端到端工作流

### 3.1 运行前输入

一次主流水线至少需要：

- `--st-file`：包含目标声明、或用于生成目标 Harness 签名的 ST 文件；
- `--function`：目标 POU 名称，参数名沿用 `function`，但可以指 `FUNCTION_BLOCK`；
- `--run-dir`：本次运行的独立输出目录。

库测试还可提供：

- `--library`：完成一次性归一化后的 ST 库；
- `--stubs`：库 intake 阶段形成的显式存根声明；
- `--compatibility-manifest`：描述多个 ST 源、原生 provider、编译参数和链接参数的 `compatibility.json`。

主流水线拒绝复用已有内容的非空运行目录；`--report-only` 是例外，它要求目录已经存在。该约束用于防止不同运行的语料、任务状态和发现证据相互污染。

### 3.2 构建阶段

流水线为目标创建全局构建目录，清理并重新生成该目标的 generated seeds，然后执行：

1. `compile_st.sh`：生成 STG、编译 RuSTy LLVM IR，并进行语义 IR 插桩；
2. `build_target.sh`：生成 Harness、准备 IR、编译对象并链接 fuzz target；
3. 从 `stg-model.json` 与 `stg-runtime-ids.json` 生成 `semantic-task-plan.json`；
4. 收集目标文件旁 `seeds/*.seed` 与构建阶段生成的种子，以内容哈希去重后放入统一 corpus；
5. 把 testcase 元数据以逐行 JSON 写入扩展名为 `.json` 的 `testcase-metadata.bootstrap.json`，供 Rust engine 还原为原生 LibAFL metadata。

主流水线强制设置 `SEMANTIST_SEMANTIC_INSTRUMENTATION=ir`，因此正常的端到端运行使用语义 IR 插桩。源码仍保留关闭语义插桩的构建路径，主要用于等价性和开发测试，不是主流水线默认模式。

### 3.3 fuzz 阶段

Python 流水线以 release 模式启动 Rust engine，同时传入：

- 目标二进制和目标名；
- 运行目录与 metadata bootstrap；
- 语义任务计划、STG 模型和 runtime ID 表；
- 每个 task + path + guide_index + target 的执行预算；
- 可选的 LibAFL 迭代数。

`--fuzz-timeout` 是包裹 `cargo run` 子进程的外层墙钟超时，可能包含尚未完成的 engine 构建时间；到时后流水线捕获超时并继续生成报告。`--timeout-ms` 属于 Rust engine 和实验入口的单次 target 执行超时，不是 Python `semantist` 主流水线的参数；后者可通过 `SEMANTIST_TIMEOUT_MS` 传给 engine。比较实验的 `--fuzz-time` 则由 runner 单独计时，不使用这条开发流水线计量正式预算。

### 3.4 报告阶段

除非指定 `--skip-report`，流水线在 fuzz 正常结束或达到外层超时后调用报告生成器。报告失败会写入事件日志并输出错误，但不会掩盖已经产生的 fuzzing 结果。报告可选择：

- 是否批量重放 `target-corpus` 中的目标语料，以及该批量重放的数量；
- 单次重放超时；结构化 finding 的去重代表输入仍另行重放；
- 是否对已确认 reproducer 做确定性最小化；
- 离线 `mock` 文本生成器或 OpenAI-compatible 摘要生成器。

LLM 只接收已有证据并组织文字，不参与种子生成、语义目标判定、漏洞确认或去重键计算。

`--report-no-replay`（独立报告入口的 `--no-replay`）只跳过 `target-corpus` 批量重放，当前 `collect_finding_records()` 仍会尝试重放结构化 finding 的代表输入，不能将该选项描述为完全禁止目标执行。

主 `semantist` 流水线未显式指定 provider 时使用 `mock`；直接运行 `semantist-report` 时 argparse 默认是 `SEMANTIST_LLM_PROVIDER`，未设置该变量则为 `openai-compatible`。因此离线重建报告时应显式写 `--llm-provider mock`，避免两个入口默认值差异造成意外网络配置错误。

## 4. 厂商方言归一化与依赖闭包

### 4.1 非破坏性 intake

SemantiST 提供独立的 ST-to-RuSTy intake 工具，用于把 CODESYS、TwinCAT 或 generic 方言源整理成可重复构建的工作区。它不原地修改输入：输出目录必须为空，原始文件被保留，转换后的文件和每项变换记录单独输出。

intake 的基本顺序是：

1. 扫描 ST 文件并屏蔽注释与字符串内容，同时保持字符偏移和行号；
2. 根据单一规则表识别方言结构，记录规则、位置、上下文和安全等级；
3. 仅应用满足安全条件的确定性文本替换；
4. 生成 transformation manifest、兼容性 manifest、空或显式的 stubs 文件、依赖报告占位和验证报告占位；
5. 另行运行 `validate_library.py`，对指定 `--pou` 执行解析/类型检查、IR 生成、Harness 构建、IR 准备、最终原生链接和外部符号检查。

`prepare_library.py` 写出的 `verification.json` 和依赖 gate 初始为 pending；完成准备不等于通过验证。validator 虽生成名为 `smoke-executable` 的文件，但当前不会执行它；所选 POU 之外的运行行为及语义等价性仍需使用者另行检查。

### 4.2 转换规则的安全分层

规则定义集中在 `dialect_rules.py`，当前共有 26 条，覆盖 CODESYS、TwinCAT 和通用变体。典型规则包括：

- `POINTER TO` 改写为 RuSTy 接受的 `REF_TO`；
- `ADR(...)` 改写为 `REF(...)`；
- 某些字符串类型长度括号改成方括号；
- 紧凑终结符、时间字面量和命名空间形式的规范化；
- 需要人工判断的缺失 `END_*`、可能改变语义的声明或调用形式。

规则被分为可自动应用、必须审核后才能应用和不支持。源码中规则 5、6、13、15、16、26 属于 review-required；`METHOD` 相关规则是不支持项，因为工具不会猜测对象方法调用与普通函数调用是否语义等价。转换器遇到未获批准的 review-required 项或 unsupported 项会阻断，而不是静默生成可能错误的程序。

替换以源码位置为依据并保留原始注释和字符串。分析器对缺失终结符等问题只报告，不通过推断补出程序结构。

### 4.3 compatibility manifest

`compatibility.json` 使用 schema `semantist.compatibility/1.0.0`，可以描述：

- 归一化 ST 源和额外兼容 ST 源；
- 原生 C/C++ 源、已编译对象和库；
- 原生编译参数和最终链接参数；
- 可复用 compatibility profile；
- RuSTy ST 标准库 glob 和运行时 archive。

加载器验证路径并按确定顺序形成 ST 编译输入。原生构建器对 C/C++ 使用 Clang，默认带 `-g -O2 -fno-omit-frame-pointer`；出现 C++ 输入时补充相应 C++ 运行库。

### 4.4 外部符号与 stubs 原则

外部依赖按如下来源检查：

1. 编译器 intrinsic，例如 `REF`、`SIZEOF`；
2. RuSTy ST 标准库；
3. RuSTy runtime archive；
4. 目标库自身；
5. compatibility runtime；
6. 显式 stubs。

stubs 被区分为声明、语义 adapter 和环境模型。工具会检查冲突和最终链接是否存在 provider：只有声明而没有可链接实现的符号仍是 unresolved。它不会为了让链接通过而自动给未知函数生成“返回零”的可执行默认实现，这避免了把缺失依赖伪装成正常库行为。

验证报告只有在已选 POU 的各项编译/链接检查、外部声明清单检查和转换审计检查通过，且依赖 gate 未 blocked 时才写为 passed。因此“RuSTy 能产生 IR”但“最终 native link 失败”仍是失败结果。其中名为 `reachable_externals` 的检查实际基于 IR 外部声明清单，不是完整调用图可达性证明；`audit_complete` 也不证明获批转换与真实 PLC 语义等价。

在实验前审核并保留验证记录是操作要求。生产 `load_project()` 当前检查 compatibility schema 和引用路径，不读取 `verification.json` 强制验收 gate；生产编译/链接错误仍会阻断，但不能声称所有未验收 intake 都被工具自动拒绝。

### 4.5 CODESYS memory compatibility profile

仓库自带 `codesys-memory` profile。它用 ST adapter 暴露 `SysMemSet`、`SysMemCpy`、`SysMemMove` 形式的接口，并由原生 runtime 提供 checked/unchecked 实现。若目标和源对象大小能够从 alloca 推断，IR 准备阶段把相应调用改写为带对象大小的 checked shim；请求长度越界时 shim 中止，从而由动态运行与 sanitizer 形成证据。无法推断大小时传入 `UINT64_MAX` 表示未知，不把“未知”错误地当作已证明安全或已证明越界。

## 5. 目标 ABI 与 testcase 数据模型

### 5.1 ABI 来源

Harness 不再解析 ST 声明文本。`compiler/harness/abi.py` 从同一次构建的两份编译器产物合成目标 ABI：

- **STG 模型（`stg-model.json`）** 提供逻辑类型。类型由 RuSTy 语义索引解析，常量、数组上下界（包括引用 `VAR_GLOBAL CONSTANT` 和库常量的界限）、字符串容量和 `VAR_IN_OUT` 指向的类型都已经求值；别名、子范围和枚举解析到其基础类型。
- **LLVM IR** 提供物理布局。FB 实例的字段集合和顺序取自 RuSTy 在 FB 入口为每个成员生成的命名 GEP；字段偏移和实例大小按 x86_64 SysV DataLayout 计算。FUNCTION 的参数位置取自函数签名，并通过入口块中写入同名 alloca 的 `store`/`memcpy` 绑定到声明变量；按值传入的字符串/数组还记录 RuSTy 实际拷贝的字节数。

两份来源共有的事实逐项交叉检查：标量宽度和浮点/整数类别、字符串和数组的字节数、按引用成员必须是指针、拷贝字节数不得超过声明容量、IR 字段必须全部有对应 STG 成员。任何不一致都报错，不再回退到猜测的布局。嵌套 FB、STRUCT 等成员按 IR 给出的大小和对齐生成不透明字段；`VAR_TEMP` 不属于实例，不出现在结构体中。

### 5.2 支持的输入类型表示

ABI 中的类型映射到以下 C 存储：

- `BOOL`、`SINT/USINT/BYTE/CHAR`、`INT/UINT/WORD/WCHAR`、`DINT/UDINT/DWORD`、`LINT/ULINT/LWORD`、`REAL/LREAL`；
- `TIME/DATE/DT/TOD` 及长名称别名，按 RuSTy 的有符号 64 位值处理；
- `STRING` 和 `WSTRING`，容量取 STG 的解析结果（RuSTy 默认 `STRING` 为 80 个字符）；
- 多维 `ARRAY`，扁平化为元素数组；
- `POINTER TO`/`REF_TO` 输入：backing storage 至少为一个声明 pointee 类型的对象（上限 1 MiB），种子值更长时扩展、更短时补零；
- 不透明聚合（嵌套 FB、STRUCT 成员）：按十六进制字节填写，seed 中的类型 token 为 `ARRAY`。

### 5.3 FUNCTION 与 FUNCTION_BLOCK 字段分类

对 `FUNCTION`：

- fuzz 参数包括 `VAR_INPUT`（含注释/修饰的 constant input）和 `VAR_IN_OUT`；
- 返回值按函数声明类型处理；
- 一次 testcase 调用函数一次。

对 `FUNCTION_BLOCK`：

- `state_fields` 是 IR 实例结构体中除 vtable 外的全部成员，按 IR 字段顺序排列；
- fuzz 参数包括 input 和 inout；
- 输出字段单独记录；
- persistent fields 是排除 input、inout、output、temp 和 constant 后的普通内部字段；
- 状态快照由 output、persistent 和 inout 组成；
- 同一 testcase 的多个扫描周期复用同一个构造后的 FB 实例。

STG 对 constant/configuration input 与普通 cycle input 有语义区分，但 Harness 的文本赋值逻辑仍会接受名称匹配的 constant input 记录。因此应把 constant 视为种子生成和任务建模上的配置字段，不应声称 Harness 在运行时强制其不可改变。

### 5.4 testcase 文本格式

每行采用：

```text
NAME,TYPE,VALUE
```

FB 的周期字段使用：

```text
0.ENABLE,BOOL,1
0.VALUE,DINT,10
1.ENABLE,BOOL,1
1.VALUE,DINT,20
```

解析赋值时以声明 schema 和字段名决定实际类型；行内 `TYPE` token 不参与 C 端类型选择。重复的同名记录以最后一次匹配为准。

值的处理规则包括：

- 整数接受十进制和 `0x`，然后按目标 C 类型转换；
- 浮点由 Harness 自带的简单解析器处理符号、小数和指数；
- `STRING` 先清零，再截断并保证终止；
- `WSTRING` 将输入的每个字节扩展为宽元素，不执行 Unicode 解码；
- 数组接受十六进制字节串或文本字节，写入固定缓冲区；
- 指针 backing storage 按 max(本次解码长度, pointee 类型大小) 重新分配。

结构化变异器用 `splitn(3)` 解析记录，因此值中后续逗号不会继续拆字段。无法解析为有效结构时，变异阶段不会把任意字节输入伪装成正常结构化 testcase。

## 6. C Harness 与动态观察

### 6.1 FUNCTION Harness

FUNCTION Harness 的 C 原型按 IR 签名逐位生成，为每个参数分配 C 存储、逐行扫描输入并赋值，然后调用一次 `__semantist_semantic_cycle(0)` 和目标函数。标量返回值写入 `volatile`；字符串、数组返回按 IR 中的返回指针参数使用显式结果缓冲区；按引用传递的 `VAR_IN_OUT`/`VAR_OUTPUT` 标量传地址。输出会以最小逃逸方式被观察，防止编译器把整个调用优化掉。

Harness 本身不嵌入“输出应该等于什么”的功能 oracle，也不植入指针 canary oracle。故障依据来自 sanitizer、进程退出、信号和超时，语义目标命中则来自插桩。

### 6.2 FUNCTION_BLOCK Harness

FB Harness 按 IR 字段顺序生成包含 vtable 指针的 C struct，并对实例大小和每个字段偏移生成 `_Static_assert`，布局与编译结果不一致时 Harness 无法编译。调用构造函数后，在同一个实例上顺序执行周期。inout 字段使用按 pointee 类型分配的单独存储并把地址交给实例。

周期记录按输入流的“周期 ID 发生变化”触发上一组执行，而不是先把整个文件按 ID 全局分组。因此记录顺序具有语义；同一个 ID 若不连续出现，可能造成多次周期执行。SemantiST 自己生成和变异的 seed 会规范化 cycle ID 与顺序，从而避免这一歧义，但手工 testcase 仍应保持周期块连续且递增。没有周期前缀的配置/inout 记录被当作共享记录，并在生成 seed 中放在周期记录之前。

基础 FB seed 对普通输入生成至少两个周期，并对 BOOL/浮点等字段给出变化值；configuration input 和 inout 作为共享记录。语义初始种子生成器还会根据任务的 temporal requirement 扩展周期数。

### 6.3 FB 状态摘要与停止条件

抽象状态观察默认启用；满足以下任一条件时，Harness 另外计算精确快照：

- `SEMANTIST_FB_STATE_TRACE` 开启；
- `SEMANTIST_FB_STALE_THRESHOLD` 大于零；
- 设置了 FB 状态 trace 文件。

默认每个周期通过共享内存记录前后抽象状态。投影检查快照字段列表的前 32 项（output、persistent 和 inout），跳过指针与非标量 inout，对其余数值分桶，对数组/字符串仅区分前 16 字节是否包含非零字节，避免连续计数器每次变化都无限扩充语料。精确字段快照保留给显式 trace/stale 诊断；连续无变化达到 stale threshold 时可提前停止。状态 trace 文件或标准错误均是调试输出，默认状态反馈不依赖它们。engine 统一生成、变异和 harness 的 `SEMANTIST_FB_MAX_CYCLES`：默认 256，最高 10000。

需要准确理解可选精确快照的边界（默认抽象投影另见算法说明）：

- 标量、数组等字段按其 Harness 存储字节参与摘要；
- 普通指针字段摘要的是指针值，而不是任意长度的 pointee 内容；
- inout 字段从 `inst->field` 指向的存储读取一个 pointee 对象的字节参与摘要；
- 摘要变化反映所观察字节变化，不等价于完整 PLC 状态等价判定。

Harness 提供弱符号 `__semantist_semantic_cycle` 和 `__semantist_semantic_state`；语义 runtime 链接后用强实现记录实际扫描序号、有序目标事件和抽象状态。固定 benchmark harness 由 `benchmarks/generate_harness.py` 从基准的编译结果生成；`build_target.sh` 用本次构建的 IR 和 STG 重新生成并要求与 fixture 逐字节一致，否则拒绝构建。

## 7. Semantic Target Graph（STG）

### 7.1 生成方式与确定性

STG crate 对项目执行两条 RuSTy 编译路径：

- source pipeline：解析、索引和 annotation，但不做 lowering，用于保留源级结构、位置和语义；
- lowered pipeline：执行默认 lowering participants 和验证，用于连接真实代码生成节点。

输入既可以是 ST 文件集合，也可以是项目目录/`plc.json`。模型 metadata 保存生成器版本、固定 RuSTy 版本/提交、project root、输入文件相对路径与 SHA-256，并标记确定性。输出集合经过稳定排序。

稳定 ID 由 SHA-256 的前 16 字节产生，输入包含：命名空间、项目相对路径、POU qualified name、源 span（文件、偏移、行列；无源位置时用 synthetic 描述）、语义 kind 和 ordinal。RuSTy 的临时 `AstId` 不进入稳定 ID，所以只要上述语义身份不变，ID 不依赖某次进程内 AST 编号。

### 7.2 项目与 POU 模型

`stg-model.json` 的顶层包含：

- `schema_version`：当前为 `semantist.stg/1.2.0`；
- `metadata`：构建器和输入指纹；
- `pous`：每个 POU 的语义模型；
- `diagnostics`：项目级诊断；
- `statistics`：项目统计。

每个 POU 模型包含 descriptor、semantic graph、environment、mappings、targets、诊断和统计。POU descriptor 指明名称、qualified name、kind、源码位置和是否 stateful。

### 7.3 图节点

节点类型包括：

- `Entry`、`Exit`：一次调用的边界；
- `CycleEntry`、`CycleExit`：有状态 POU 的扫描周期边界；
- `Predicate`：`If`、`Elsif`、`Case`、`While`、`Repeat`、`For`；
- `Outcome`：谓词或控制结构的具体结果；
- `LoopLatch`：循环回边汇合点；
- `LoopControl`：显式 `EXIT`、`CONTINUE` 等控制点；
- `Hazard`：危险操作位置；
- `PropertyViolation`：为属性前端预留的节点类型。

Outcome role 包括 true/false/else、case label/default、loop enter/continue/exit/back、explicit exit/continue、return 和 cycle entry/exit。Outcome value 可以是布尔值、表达式、default 或 structural。

每个节点还保存可用的源码位置、AST origin、可选 hazard 和静态特征。静态特征包括嵌套深度、表达式复杂度、输入与状态依赖、所属循环以及调用数量等。

### 7.4 图边与 transfer

边分为：

- `Evaluation`：带 typed guard 的谓词求值路径；
- `ControlTransfer`：带 transfer summary 的语句执行路径；
- `Temporal`：从一个扫描周期到下一个周期的状态关系。

transfer summary 包含建模状态、操作序列、read/write 集、输入/输出版本以及未修改变量的 hold 语义。操作类型包括：

- `Assign`：目标、目标 symbol、值表达式、读取集、版本变化、类型转换和源码位置；
- `Call`：callee、参数、调用 effects 和源码位置；
- `Return`；
- `Allocation`；
- `Opaque`：无法精确解释的 AST 类型、原因和可选位置。

变量版本是在局部控制流路径上推进的，用于表示赋值前后关系，并非完整 SSA IR。def-use 由赋值和调用 effects 构造。已知内部调用按签名保守计算读写；未解析或外部调用标为 opaque/conservative；by-reference 和 output 参数可形成写效果。当前模型不会为了“看起来完整”而虚构被调用函数体的跨过程语义。

### 7.5 表达式 DAG 与类型事实

environment 中保存 symbol、type、表达式、def-use、调用和 persistent state 等事实。表达式是有类型 DAG，种类覆盖：

- literal、variable；
- binary、unary；
- call；
- array、member；
- pointer、address；
- cast；
- direct/hardware access；
- range、list、case；
- opaque。

literal 不只保存普通整数/实数/布尔/字符串，还区分 wide string、duration、date、time-of-day、date-time、null 和 array literal；时间类值使用纳秒表示，无法求值时相应字段可为空。

symbol 事实保存 qualified name、类型、变量 role、constant、retain、by-reference、初值与配置来源。配置来源只使用已经识别的 IEC constant 或 `VAR_INPUT (* CONSTANT *)` 注释约定；STG 的注释约定扫描要求该标记与 `VAR_INPUT` 位于同一源码行。类型事实可保存物理 bit width、semantic bit width、signedness、数组维度、结构字段、字符串编码/容量、pointer target 和 type-safe-pointer 标记。类型类别覆盖 integer、float、boolean、array、string、struct、pointer、enum、subrange、alias、interface、void 和 generic。

类型事实来自 RuSTy annotation/type system。`layout_offset_bits` 和结构字段 `offset_bits` 都是可选值；STG 不凭轻量 Harness parser 猜测 ABI offset。没有可靠类型或表达式语义时使用 conservative/opaque 状态。

mappings 还分别保存 semantic node 和 expression 到 source/lowered AST ID 的关系，并为未来 IR location 预留 module、function、block 和 instruction 字段。这里的 AST ID 用于一次构建内连接 source/lowered pipeline，不进入 stable ID。

### 7.6 建模可信度

几乎所有关键语义对象都携带以下状态之一：

- `Exact`：该对象在当前静态模型中具有明确语义；这不自动保证存在 IR 探针映射，也不是形式化等价证明；
- `Conservative`：信息可用于引导，但不能作为已精确证明的违反条件；
- `Opaque`：工具无法在当前模型中解释。

这一状态沿 transfer、call effects、type conversion、target 和 task 传播。运行时把 exact violation 与 conservative violation 区别对待：后者不会仅凭语义 bitmap 命中就升级为漏洞 objective。

### 7.7 模型校验与诊断

STG 生成后执行结构和语义一致性检查，主要包括：

- 每个 POU 内 node、edge 和 target ID 唯一，边端点存在；当前没有额外的全项目 expression ID 唯一性证明；
- evaluation edge 具有 guard，control-transfer edge 具有 transfer summary；
- predicate 具有预期 outcome role 和对应 evaluation edge；不对 guard 的互斥性、逻辑完备性做求解证明；
- 循环具备 exit/back 语义和相应目标；
- cycle relation 存在，模型中的 persistent 集不与 cycle-input/inout/configuration 集相交，且等于 temporal edge 保存的 persistent 集；不据此证明完整 PLC 状态分类正确；
- expression operand 和 type 引用存在；
- assignment 引用存在，改变类型时有显式 conversion 记录；不证明所有转换语义正确；
- target 的 node 引用及 source-AST mapping 条目存在，target edge 列表非空且引用有效；不证明这些映射与真实 IR 语义等价。

诊断带 error/warning/info、code、POU、semantic ID 和源码位置，统计中分别记录 error/warning、conservative/opaque summary 等计数。当前 `generate()` 会把诊断写进模型并继续生成主 artifacts；它不因模型中出现 validation error 就自动抛出构建失败。因此评估 STG 质量时必须检查 `diagnostics`/`error_count`，不能仅以 `stg-model.json` 文件存在作为模型完全有效的证明。开启 debug sidecars 后还会单独输出 diagnostics、statistics；DOT 同样是显式调试开关。

校验能力的逐项边界见 [STG 模型说明第 13 节](stg_semantic_model_zh.md#13-模型验证约束)。

## 8. 控制流语义建模

### 8.1 IF/ELSIF/ELSE

每个 `IF`/`ELSIF` 条件按 ST 语义构造 predicate 及 true/false 结果。true 结果进入对应 body；false 结果连接到下一个 `ELSIF` 或 `ELSE`；没有显式 `ELSE` 时仍形成结构性剩余路径。校验器检查预期 outcome role 和 evaluation edge 是否存在，不额外证明 guard 的逻辑互斥与完备。

### 8.2 CASE

每个 label（包括 range）形成独立 case-label 目标。default guard 被建模为所有显式 label 不成立的合取/否定关系，而不是一个没有条件的普通边。因此调度器可以分别引导具体 label 和剩余空间。

### 8.3 WHILE

条件 true 对应 loop-enter，false 对应 loop-exit。body 末尾进入 latch，再形成 loop-back 到 predicate。显式退出路径不经过正常 back 语义。

### 8.4 REPEAT

`REPEAT` 先结构性进入 body，再在尾部求值：条件 true 表示退出，false 表示继续，随后经 latch/back 返回。它与 `WHILE` 的先判断语义没有被合并成同一种图模板。

### 8.5 FOR

模型显式包含初始化赋值和 latch 增量。为兼容正、负 step，循环条件综合为：

```text
(step > 0 AND counter <= end)
OR
(NOT(step > 0) AND counter >= end)
```

随后生成 enter、exit、latch 和 back 目标。该条件是 STG 的语义表达，不是从某个 LLVM 比较指令的形状反推出来的。

### 8.6 EXIT、CONTINUE 与 RETURN

显式 `EXIT`、`CONTINUE` 都有自己的控制目标与边；`RETURN` 终止当前流点并转向一次调用的边界。对 stateful POU，返回连接本次 invocation/cycle 边界，而不是误接到任意后继语句。

### 8.7 有状态 POU 的周期关系

FUNCTION_BLOCK 的 symbol 被区分为：

- configuration input；
- 每周期普通 input；
- inout；
- output；
- persistent internal state；
- temp/constant 等其他类别。

模型用一条 `CycleExit -> CycleEntry` temporal edge 表示相邻扫描周期，携带的集合严格是 persistent state。普通 cycle input 不沿 temporal edge 继承。目标的反向依赖分析遇到 temporal edge 时停止；需要前一周期准备状态的关系由任务规划器显式生成多周期 requirement，而不是把跨周期依赖误当作单周期数据流。

## 9. 语义目标与危险操作

### 9.1 目标类型

STG target kind 包括：

- branch；
- loop；
- case；
- control；
- cycle；
- hazard reach；
- hazard violation；
- external output；
- property violation。

property violation 的模型类型和运行时分类已经存在，但当前仓库没有一个通用用户属性语言前端。因此不能把它描述成已经支持任意用户断言。

### 9.2 reach 与 violation 分离

危险操作通常生成两个目标：

- reach：动态执行到该危险操作；
- violation：危险前提在该次执行中成立。

这样即使 violation 的 IR 条件无法安全构造，reach 仍可能作为可映射 GuideTarget，帮助 fuzzer 先到达相关代码。external output 是例外，它只生成 external-output 目标，不创建人为的“违反”目标。

### 9.3 当前危险类别

源码从有类型表达式中提取：

- 除法 `/`；
- 取模 `MOD`；
- 有限位宽整数或浮点的加、减、乘；
- 幂运算；
- 数组索引；
- 指针解引用；
- cast；
- 赋值中的潜在 narrowing conversion；
- external output assignment。

除法、MOD、索引、解引用等可具有 exact 模型；幂运算和部分 cast 是 conservative。普通有限位宽算术在类型信息足够时是 exact，指针算术则降为 conservative。narrowing assignment 比较源/目标语义宽度：常量可精确处理，动态值的违反建模更保守。

目标的 backward dependencies 从目标反向遍历 evaluation/transfer 边，递归追踪 def-use，同时收集输入、状态和 transfer chain。它不会跨 temporal edge 无限展开；多周期关系由后续 planner 处理。

## 10. 从 STG 到 LLVM IR 的跨层映射

### 10.1 runtime ID

稳定 target ID 适合跨构建引用，但不适合作为 bitmap 下标。构建器将目标按稳定 ID 排序，分配从 0 开始的稠密 `u32` runtime ID，并写入 `stg-runtime-ids.json`（schema `semantist.runtime-ids/1.0.0`）。每项保留稳定 ID、POU、node/edge、kind、role、hazard、源码位置、建模状态和 violation predicate。

### 10.2 codegen map

`stg-codegen-map.json`（schema `semantist.codegen-map/1.0.0`）把源级目标连接到 RuSTy codegen anchor。映射可以是 exact、one-to-many 或 unmapped。普通控制目标依据 lowered AstId 和源码 anchor 关联；cycle entry/exit 可使用 function anchor。对于常见布尔分支，true/enter 提供 successor 0 hint，false/else 提供 successor 1 hint；循环根据其 outcome 做相应处理。

### 10.3 RuSTy 语义元数据补丁

仓库的 RuSTy patch 在真正生成 LLVM 指令时读取 codegen map，并为相应指令附加 `!semantist.semantic` JSON metadata（schema `semantist.llvm-semantic/1.0.0`）。覆盖的 anchor 包括函数进出、条件、switch label/default、循环进入/回边、exit/continue/return、表达式和赋值危险点。

查找优先使用 POU + lowered AstId，必要时回退到源偏移和 function anchor。这样 IR 映射来自真实 codegen 位置，而不是对优化前后 LLVM 文本做脆弱的源码重解析。sidecar 缺失、JSON 错误或 schema 不匹配会产生 E101--E103 类日志并得到空映射，后续插桩诊断会反映目标未映射。

### 10.4 IR 探针插入

Rust IR instrumenter 读取带元数据的 LLVM IR 和 runtime ID 表。对于 CFG terminator：

1. 按 successor 聚合同一目标的事件；
2. 为真实边建立独立 probe block；
3. 在 probe block 调用 semantic hit/hazard runtime；
4. 把原 terminator 对应边重定向到 probe block，再由 probe block 跳到原 successor。

即便两条语义边落到同一目标基本块，也各自拥有探针，因此不会因 destination 相同而丢失 outcome 区别。终结型事件可在指令前插 hit；hazard reach 和 external output 调用普通 hit；exact violation 在能够构造条件时调用 `__semantist_semantic_hazard(runtime_id, condition)`。

插桩后模块会重新验证。mapping artifact 记录函数、基本块、指令 ordinal、successor、destination、probe 和目标类型；未映射或无法构造违反条件会写入诊断。存在 unmapped 诊断本身不一定使整个命令退出失败，所以构建成功不能等同于每个语义目标均有可用探针。

### 10.5 动态 violation 条件

当前 IR 端可构造的主要条件为：

- 整数除/余：divisor 为零；有符号整除还检查 `MIN / -1` 溢出；
- 浮点除/余：有序比较等于零；
- 指针 load/store：指针为空；
- 整数加/减/乘：调用对应 signed/unsigned LLVM overflow intrinsic；
- 浮点加/减/乘：结果为 NaN 或正/负无穷；
- 数组：从保留下来的 GEP/type 形状推导扁平元素总数，检查最后一级索引 `< 0` 或 `> count - 1`；
- truncation：对截断值符号扩展或零扩展，再与原值比较；常量 store 可依据 metadata 折叠。

若精确条件无法从当前 IR 安全重建，插桩器产生诊断并把 violation 留为 unmapped。特别是数组检查是扁平计数并依赖 GEP 形状，不能声称覆盖所有多维布局、动态对象或厂商 ABI。

### 10.6 语义 runtime

C runtime 从环境读取独立的 System V shared-memory ID 和 map size。普通 hit 对对应 `u8` counter 做饱和加一；hazard 只有在传入 condition 为真时才 hit。独立 40,976 字节遥测区默认记录真实扫描次数、最多 4096 个有序目标事件和 512 对前后抽象状态，父进程逐输入清零；容量溢出显式标记，不能把截断尾部当完整路径证据。可选逐 hit JSONL trace 仍会打开、追加并关闭文件，仅用于调试。

## 11. 目标 IR 准备与最终构建

RuSTy 编译先在 `artifacts/build/st-projects/<target>/` 形成 `target.compiler.ll`，语义 instrumenter 输出目标构建目录中的 `target.ll`。`prepare_target_ir` 再产生 `target.prepared.ll`，并在目标编译前执行：

- 去除 LLVM 版本兼容问题，例如 GEP 的 `nuw` 和 `captures(none)`；
- 可选移除 global constructors；
- 用 `internalize,globaldce` 保留目标函数和必要构造入口；
- 默认运行 LLVM `bounds-checking<rt-abort>`；
- 默认把能够识别对象大小的 SysMemMove/Cpy/Set 调用改写到 checked shim。

最终使用 LLVM 21 Clang 以 `-g -O1` 构建，保留 frame pointer，默认启用 ASan、UBSan 与 AFL `trace-pc-guard`。链接对象包括 Harness、目标 IR 对象、AFL runtime、RuSTy stdlib/runtime、compatibility 原生输入、语义 runtime，以及 `dl`、`pthread`、`m` 等系统库。

这里描述 `build_target.sh` 的通用默认值。当前比较实验的 SemantiST adapter 显式把 `SEMANTIST_TARGET_SANITIZERS` 设为 `-fsanitize=address -fno-omit-frame-pointer`，因此该实验构建不能被写成默认同时开启 UBSan；应以每次保存的编译命令和配置为准。

编译项目时，工具创建临时 `plc.json`，组合标准库 glob、compatibility ST 源、library、stubs 与目标文件。如果选定 POU 已经位于 library 中，目标 ST 文件只用于 Harness 声明，不重复作为 library 编译单元加入，避免重复定义。

## 12. 语义任务规划

### 12.1 任务输入与分类

planner 读取 `stg-model.json`、runtime ID 表和相邻 IR mapping，输出 schema 为 `semantist.semantic-task-plan/1.0.0` 的任务计划。任务始终使用稳定 target ID；runtime ID 只负责执行时映射。

目标分为：

- terminal：hazard violation 和 property violation；
- guide：branch、case、loop、control、cycle、hazard reach；
- external output 保留为可观察语义目标，但不是凭本身确认漏洞的 violation。

每个 exact 或 conservative terminal 都保留 violation task，即使 terminal 本身 unmapped 或当前没有可用路径。若 violation unmapped 而成对的 reach 可映射，planner 生成 reach exploration task，使危险位置仍可被引导。尚未被 terminal task 路径吸收的 guide targets 也形成 exploration tasks。

### 12.2 候选路径

planner 对每个 POU 使用确定性的 Tarjan 算法划分 strongly connected components，然后做有界 BFS：

- 普通任务最多保存 8 条候选路径；
- exploration 任务最多保存 4 条；
- 单条路径不重复节点；
- 路径不允许离开一个 SCC 后再次进入该 SCC。
- 每次搜索默认最多展开 4096 条边，拒绝的循环边也计数；`path_search` 保存实际展开数、队列峰值和截断原因。

这种表示保留循环“需要到达/回边”的语义，但不会把循环无限展开或固定成某个迭代次数。路径目标还会按 STG edge correspondence 过滤；terminal 必须位于末尾，violation task 会确保成对 reach 在 violation 前。

### 12.3 任务内容

每个任务可包含：

- candidate paths 与当前 active path；
- 按路径顺序排列的 waypoint targets；
- control obligations，以及展开后的 guard expression tree；
- terminal goal expression；
- 输入/状态 data dependencies；
- 与依赖或 persistent state 有关的 transfer chain 和展开后的 operations；
- temporal requirement；
- hazard prerequisite；
- controllable seed fields 及其 type facts；
- 建模状态、runtime ID、kind/hazard；
- 初始状态和命中后可解锁目标。

如果 dependency 与 persistent state 相交，temporal requirement 至少需要两个周期。configuration input、普通 cycle input 和 inout 会保持各自作用域，供种子生成与周期变异使用。

### 12.4 effort、gain 与优先级

实现把任务剩余难度分成五个归一化维度：

```text
ec = 当前 seed 有序前缀之后的 control obligation 数
ed = dependency 数 × (1 - seed affinity)
es = transfer 数 × (1 - state progress)
et = 尚未满足的 temporal cycles
eh = 当前 seed 有序前缀中尚未出现的 hazard prerequisite + terminal
```

各维度按当前 POU 的最大值归一化后求和，得到候选 seed/路径的 effort。Rust runtime 使用实际扫描次数减一作为 temporal progress，与各并行要求 `min_cycles - 1` 的最大值比较。affinity 是 seed 与任务 waypoint 集合重叠的启发式量；只有该 seed 存在状态签名时 `state_progress` 才取 affinity，否则为零。这些量不能替代有序前缀，也不是数值距离或 transfer 已满足的证明。gain 中的未发现目标使用全局集合，与这里的单 seed 前缀成本分开。

gain 由 exact、尚未覆盖的 violation，加上尚未覆盖的 unlocked targets，再加 control kind 多样性组成；多样性按七类归一并上限为 1。基础优先级为：

```text
P_base = gain / (1 + min_effort)
```

失败预算会指数衰减：

```text
P_dynamic = P_base × exp(-0.25 × failed_attempts)
```

这套分数用于任务与 seed 排序，不是漏洞概率或静态可达性的证明。

### 12.5 任务状态机

任务状态包括 blocked、ready、active、covered、deferred 和 unmapped。active candidate path 以“当前 seed 的有序前缀之后剩余 waypoint 最少、路径最短、稳定 ID 字典序”确定。事件按 waypoint 子序列匹配，重复目标按路径位置计数，不合并不同输入证据。更换 seed 后重算路径和 guide；一个变异阶段内 guide 位置固定，下一阶段才从实际保存的 seed 重算。

匹配可跨同一输入的多个扫描，目前未把候选 temporal edge 与实际扫描边界逐项绑定。因此该前缀是引导启发式，不能证明同一扫描控制路径或完整状态转移链可行；违规终点仍要求真实探针命中。

每个 task + path + guide_index + target 默认有 1000 次实际执行预算，可由 `--semantic-task-budget` 修改，以变异阶段为结算窗口。同一目标的不同路径位置分别计费；预算归属真正执行的上一阶段，成功窗口先清该键预算，普通 queue 工作不消耗旧语义任务预算。连续三次失败后任务 deferred；新语义覆盖、新 corpus 或 128 次调度的冷却到期可重新激活，每 32 次调度检查冷却。covered 和 unmapped 分别表示终点已观察到或缺少动态探针映射。

## 13. 初始语料生成

Rust semantic generator 要求 seed 目录中至少有一个 Harness 可接受的结构化基础 seed，并选择记录数最多者作为模板。它只对 STG 中的 input/inout 字段生成候选，并把 STG type facts 与基础 seed schema 对齐。

### 13.1 值 profile

值候选包括：

- Safe；
- Initial；
- Zero、One、Negative；
- Lower、Upper；
- Constant。

常量从表达式/任务中按类型收集，数量有界；每字段也限制候选数量。若字段作为除数出现，Safe profile 强制非零。整数上下界来自语义位宽；字符串 pattern 长度限制在 1--16；数组和指针初始字节 pattern 限制在 1--8，避免初始 corpus 爆炸。

### 13.2 周期 seed

先写共享 configuration/inout，再逐周期写该周期全部输入字段。周期数取基础 seed 周期、语义 minimum 和 2 的最大值，再限制到统一 `SEMANTIST_FB_MAX_CYCLES`；不再硬编码为 8。生成器构造全局 profile、单字段 upper/constant 以及跨周期交替/pattern 等有限候选。

FUNCTION 最多保留 16 个生成候选，FB 最多 24 个；最后用 SHA-256 内容哈希去重。候选仍需交给 LibAFL 执行和 feedback 决定是否 admitted，生成数量不等于最终 corpus 数量。

若初始加载没有形成可用 corpus，engine 还会尝试结构化、非零的 fallback variants。如果所有启动输入都成为 objective，但 findings 已经存在，engine 会正常停止而不是无种子 panic。

## 14. Seed profile、增量排名与调度

### 14.1 SeedProfile

每个被接受 seed 的语义 profile 来源于 observer event，而不是仅扫描 seed 文本。profile 包括：

- 命中的稳定 target ID；
- target 按 cycle 的命中；
- 实际执行的扫描次数/序号；
- 有序目标事件及轨迹完整性标记；
- 当前输入的有界抽象状态/转换摘要；
- 对各 task 的 affinity；
- parent/source 等元数据。

默认共享内存提供周期、有序事件和抽象状态，不依赖逐 hit trace。当前状态画像与全局新增状态分开保留：重复已见状态仍属于当前 seed。本次最多保留 256 个状态/转换签名，全局最多记忆 4096 个；到达上限后其他覆盖 admission 继续有效。抽象状态不包含扫描序号，重复相同状态不会单靠序列变长持续产生新颖性。

### 14.2 每任务候选缓存

planner/runtime 为每个任务维护按 ranking 排列的 seed 缓存，最多 4 个。缓存只对 dirty task 增量重建，避免每次调度全量扫描 corpus。若缓存中的最佳 seed hash 已不在 corpus，会使排名失效并重建。

非探索模式下，任务 ranking 和每任务 seed ranking 都只在前四名中按 8、4、2、1 加权抽样。任务探索分支可从全部 selectable tasks 均匀抽取，seed 探索分支可扩大到全部已 profile 的 seed 均匀抽取，因此排名不会完全排除低位候选。

### 14.3 Target-aware scheduler

调度概率由源码常量固定：

- 75% 选择语义任务路径，25% 使用普通 queue 调度；
- 在语义分支中保留 15% 的 target exploration；
- seed 选择保留 20% exploration。

这些比例描述实现策略，并不自动说明它们是全局最优超参数，论文中应通过消融或敏感性实验验证。

engine 使用有界 `ContentHashInputFilter`，以最多 65,536 个 u64 指纹进行 FIFO 去重，淘汰后允许再次执行。它是有碰撞可能的轻量过滤，不是永久精确执行缓存；持久 seed 身份仍为 SHA-256。

## 15. Feedback、objective 与 coverage epoch

### 15.1 两张覆盖图与独立遥测

target 进程同时连接：

- 固定 65536 字节的 AFL edge map；
- 大小为 `max(runtime_id)+1` 的 semantic map。
- 独立 40,976 字节周期/事件/抽象状态遥测区。

两张覆盖图和遥测区由各自 observer 读取，不把语义目标哈希进 AFL map。因此 AFL 边覆盖反馈与语义目标覆盖可以分别观察；这些内部覆盖口径不能直接替代跨工具的公共覆盖重放结果。

### 15.2 corpus interesting feedback

普通 corpus admission 使用 OR 组合：

- coverage epoch observer：记录 AFL 覆盖进展，但自身返回 false；
- semantic feedback：新稳定 target、新状态摘要，或活动路径首次更长的连贯有序前缀；
- LibAFL MaxMap edge feedback；
- TimeFeedback。

因此一次输入即使没有新语义 target，也可能因新 edge map 最大值、状态、前缀或执行时间特征进入 corpus。语义证据采集位于 objective 分类之前，正常 corpus 和 solution 两种执行都更新全局覆盖及本次画像；普通 feedback 只消费已计算的新颖性。cycle target 计入 state 类覆盖，其余目标计入 branch 类覆盖。默认汇总日志只追加新增目标和状态，不为每次重复命中写 JSON。

### 15.3 objective feedback

solution/objective 使用 OR 组合：

- exact semantic violation/property target；
- crash；
- OOM；
- timeout；
- sanitizer finding。

conservative violation 不会单独成为 objective。这一边界防止静态近似被报告成已确认漏洞。语义 objective 按稳定目标及诊断类别去重，不能据此直接报告根因级 unique bugs。运行时故障则不要求先命中某个语义 violation。

语义 objective 会进入 LibAFL 的 on-disk target corpus；结构化 finding recorder 主要针对 crash/OOM/timeout/新增 sanitizer 日志。因此只以正常退出命中的 exact semantic objective 可能先存在于 target corpus，报告器在缺少结构化 finding 时通过回放补证据。

### 15.4 coverage epoch 与失败恢复

实现维护 AFL 覆盖 epoch、语义目标 coverage epoch、corpus epoch 和调度选择 epoch。语义目标的新发现、新 corpus 或有界冷却都可唤醒 deferred 任务，避免没有新静态边时永久沉睡。状态/前缀新颖性不冒充新静态目标，因此不会增加语义目标 coverage epoch。

任务 state 只在 semantic coverage/corpus epoch、active task、ranking 或 attempt history 等签名发生变化时用临时文件加原子 rename 重写。默认 compact JSON 只保存 ranking 前 32 项及总数，不展开全部 target、task state 和 attempt history；开启 `SEMANTIST_SEMANTIC_STATE_DETAIL` 后才保留这些完整细节，适合调试而非高吞吐 benchmark。

## 16. 结构化与语义变异

### 16.1 总体选择

变异器先解析 testcase 记录并保持字段名和类型。目标上下文可用时，以 70% 概率执行 semantic mutation；其余执行通用 typed/cycle mutation。一次 fallback 变异堆叠 1 到 `min(record_count, 4)` 个操作。

### 16.2 表达式引导

内置的小型表达式求解/反推器限制深度为 16，支持：

- 比较目标；
- `NOT`、`AND`、`OR`；
- 双边范围；
- 整数/数值常量求值；
- 对局部 `+`、`-`、`*` 定义做有限反向传播。

它不是通用 SMT solver，也不宣称解任意 ST 表达式。整数比较使用 i128 和 IEC 类型边界，严格关系使用离散邻值；REAL/LREAL 使用对应类型的相邻可表示值和可往返文本。AND/OR 的有限尝试及分母反推会验证最终局部表达式；无解、不支持或未改变输入时回退。变量接受明确 variable 节点，以及成员名、完整 reads 和类型一致的单变量 member_access 包装；不将 call、cast、array 或复杂成员访问当作普通变量。定义按限定符号及已选路径解析，已写入的 seed 字段或歧义定义停用精确求值/反推。

### 16.3 危险定向变异

针对不同任务，变异器会采用与动态 condition 对齐的构造：

- division/MOD violation：把实际 denominator expression 相关字段推向 0；reach 任务推向 1 等安全可达值；
- arithmetic/conversion：使用目标类型边界；浮点可成组设置 min/max 等压力值；
- array index：尝试 `-1`、`0` 和边界；
- pointer：在 `0x` 与非空字节如 `0x00` 之间变化；
- temporal/calendar：使用时间纳秒表示和日期、日/月边界值；
- string/array/pointer：改变有效长度和字节 pattern。

controllable fields 来自任务计划，而非仅凭变量名猜测；字段名启发式主要存在于 fallback 操作。

### 16.4 周期和状态变异

变异器把记录分成 shared 部分与按 cycle ID 排序的周期 map，并在输出时规范化 ID。生成、hint 和增长操作使用统一周期上限，默认 256、最高 10000。hazard/goal 定向值通常施加到最后周期，以允许前置周期准备状态。

完整配置在有多周期 hint 或可控 transfer 输入时，以 15% 的条件概率先尝试增长前缀：复制一个非末周期并插在其后，每次只增加一个周期，保持原末周期输入。未在该分支返回时，有 transfer 字段的上下文以 50% 的条件概率尝试修改某个非末周期中、由选定路径 transfer 的读取集关联到的输入；末周期 goal 变异失败后还会再次尝试此前缀变异。这些是分支内概率，不是所有实际执行中固定的操作占比。它不会直接写内部状态、求解完整跨周期 transfer 链，或保证其他前置条件保持。

周期操作包括：

- 修改某周期值；
- 复制周期；
- 删除或插入周期；
- reset-like cycle；
- enable toggle；
- 读写交替的四周期 pattern；
- 重新规范化。

语义分支会先补足受上限约束的 cycle hint，且其删除操作检查最低周期要求；通用 cycle havoc 仍可缩短序列，因此不是所有变异都保证维持任务最低周期数。通用类型变异中字符串、数组和指针随机内容通常限制为最多 64 字节，以控制输入和执行开销。

### 16.5 当前运行时消融

消融配置在同一个 engine 中切换，目标 ABI、公共预算和反馈采集保持一致：

| 配置 | 相对 `full` 关闭的机制 |
| --- | --- |
| `no_semantic_init` | STG 语义初始候选生成 |
| `no_targeted_mutation` | 语义定向变异入口，保留语义任务调度 |
| `queue_havoc` | 语义初始生成、语义任务调度和定向变异 |
| `no_transition_guidance` | 由 transfer 读取集引导非末周期输入变异 |
| `no_prefix_growth` | 上述 15% 专用前缀增长分支 |
| `no_multicycle_guidance` | 上述 transfer 引导与专用前缀增长两项 |

这些配置都保留状态/语义反馈和通用结构化周期操作。尤其 `queue_havoc` 不是 AFL++ 替代实现，`no_prefix_growth` 不禁止通用复制/插入周期，`no_multicycle_guidance` 也不是单周期 Harness。运行方式与比较解释见 [消融实验说明](../experiments/ablations/README.md)。

## 17. Corpus、事件与发现记录

### 17.1 统一 corpus

运行期 corpus 使用 `InMemoryOnDiskCorpus`：内存索引配合 `unified-corpus/runtime` 的持久文件；solutions 使用 `OnDiskCorpus` 写入 `target-corpus`。初始和运行时 seed 都按内容哈希处理。

Python corpus store 对记录名和类型做规范化，校验预期字段、数值范围、字符串/字节容量，并维护 bootstrap metadata。源码中从目标 ST 前 20 行粗略提取的 `IF`/`MOD`/`WHILE` 文本只作为辅助 metadata，不是 STG planner 的语义来源。

### 17.2 RuntimeFindingFeedback

结构化 finding recorder 在以下情况下记录：

- crash；
- OOM；
- timeout；
- 新出现的 ASan/UBSan 日志。

它要求 testcase 仍是有效结构化 seed，并在单进程内按 seed hash 去重。默认 sanitizer 配置使错误中止、关闭 leak 检查并写日志到 findings；日志指纹综合路径、大小和 mtime，同时忽略 ptrace 环境造成的 leak-sanitizer 噪声。

每个 finding 保存 reproducer `.seed`、JSON metadata 和事件，按来源区分 initial、semantic generation 或 fuzzing。原生 `RuntimeFindingRecord` 当前保存执行计数、退出/日志类别、sanitizer 证据、输入哈希、复现路径，以及由输入文本推断的周期 ID/数量；该周期数不是实际已执行扫描数。默认记录不自动填入稳定 target、hazard 或 source location，不能因为报告 schema 能容纳这些字段就声称每条 finding 都有它们。

可选 FB state trace 提供附加摘要，但它是追加式文件流；finding 读取时可见的摘要不具备严格的每 testcase 隔离。默认共享内存画像具有逐执行清零的边界，它和 finding 中可选的文件 trace 摘要是两个来源，不应混为一谈。

confidence 主要由 sanitizer 或 forkserver exit 形成；severity 对 crash/OOM/sanitizer 为 high，对 timeout 为 medium。这是工具内报告分级规则，不是行业通用 CVSS 评分。

## 18. 回放、去重、最小化与报告

### 18.1 确定性证据优先

报告器读取运行目录、事件、语义 task state、结构化 findings 和 target corpus，使用能定位到的目标与可用 symbolizer 重放。结构化记录的最终 `replay_status` 由重放结果决定：crash、timeout、nonzero、oom 映射为 confirmed，ok/replay_error 映射为 unstable；缺少目标或代表输入时保持 unchecked/candidate。原生 finding 的 `confirmed_by_forkserver_exit`/`confirmed_by_sanitizer_log` 是发现来源，不能代替这层重放确认。重放确认也不自动证明两个输入属于不同根因，或故障具有现实 PLC 安全影响。

如果存在结构化 finding records，报告器优先使用它们，不再把 legacy target replay 证据混入同一主集合。只有没有结构化 records 时，才从 target corpus 回放形成 fallback 证据。

报告中的 `raw_objective_count` 当前相加 target-corpus 文件数和原生 finding record 数，同一输入可能出现在两处；它是原始制品计数，不能直接当独立漏洞数。去重组数量也仍受可用签名字段限制，需要根因分析才能形成论文中的 unique bug 统计。

### 18.2 分层去重

去重键按可用证据分层：

1. semantic：function + stable target + hazard + source；
2. runtime：function + exit/signal + stack/source；
3. fallback：function + exit + target kind + cycle count。

fallback 记录 seed hash 供追踪，但故意不把 seed hash 放入去重键，避免同一故障仅因输入字节不同重复计数。每组最多保存三个代表 seed；当前报告器通常重放首个代表 seed 后写入该组的状态，不能声称已重放全部代表输入。

### 18.3 确定性最小化

最小化器先规范化 seed 并建立 baseline。候选必须保持相同大类状态；若有 sanitizer，则还比较 sanitizer kind/stack hash，若有信号则保持信号一致。缩减顺序大体为：

1. 删除较后的完整周期；
2. 删除重复周期；
3. 删除单条记录；
4. 缩小字段值。

随后通过把字段恢复默认值进行 critical/maybe/irrelevant 归因。最小化是基于重放行为的 delta-style 过程，不是静态证明某字段与漏洞有因果关系。

### 18.4 输出与 LLM 边界

报告输出 JSON、普通 Markdown、正式 Markdown 和 HTML，汇总 corpus、事件、metadata、任务尝试、周期信息和最小化结果。OpenAI-compatible provider 从环境取得 API key/base URL/model，以 temperature 0 和有限重试调用；`mock` 完全离线。

提示词要求不得虚构，但无论选择何种 provider，LLM 文本都只是确定性证据的摘要层。漏洞是否确认、去重、severity/confidence 和 reproducer 均由本地数据决定。

## 19. 运行目录与主要制品

默认 artifact root 为仓库下 `artifacts`，可用 `SEMANTIST_ARTIFACT_DIR` 改写。建议布局为：

```text
artifacts/
├── build/
│   ├── st-projects/<sanitized-target>/
│   │   └── target.compiler.ll
│   └── targets/<sanitized-target>/
│       ├── target.ll
│       ├── target.prepared.ll
│       ├── target.o
│       ├── target.semantic-runtime.o
│       ├── harness.c
│       ├── fuzz_target
│       └── generated-seeds/
├── cargo-target/
└── runs/<run-name>/
    ├── events.jsonl
    ├── testcase-metadata.bootstrap.json
    ├── semantic-task-plan.json
    ├── semantic-task-state.json
    ├── semantic-coverage.jsonl
    ├── stg/
    │   ├── stg-model.json
    │   ├── stg-runtime-ids.json
    │   ├── stg-codegen-map.json
    │   ├── stg-ir-mapping.json
    │   ├── stg-manifest.json
    │   └── [debug diagnostics/statistics/build manifest / dot]
    ├── unified-corpus/
    │   ├── seeds/
    │   └── runtime/
    ├── target-corpus/
    ├── findings/
    └── vulnerability-report/
```

构建目录按 sanitized target 名共享，不是每个 run 的私有副本；STG、任务、corpus、findings 和报告位于 run 内。进行并行 benchmark 时，应避免多个进程同时构建同名 target 到同一 artifact root，或为它们配置隔离的 artifact root。

主 schema/制品关系如下：

| 制品 | Schema/作用 |
| --- | --- |
| `stg-model.json` | `semantist.stg/1.2.0`，源级完整语义模型 |
| `stg-runtime-ids.json` | `semantist.runtime-ids/1.0.0`，stable ID 到 bitmap ID |
| `stg-codegen-map.json` | `semantist.codegen-map/1.0.0`，STG 到 codegen anchor |
| LLVM metadata | `semantist.llvm-semantic/1.0.0`，真实 IR 指令上的事件 |
| semantic IR mapping | 插桩后的函数/块/指令/边/探针位置 |
| `semantic-task-plan.json` | `semantist.semantic-task-plan/1.0.0` |
| `semantic-task-state.json` | `semantist.semantic-task-state/1.0.0` |
| `compatibility.json` | `semantist.compatibility/1.0.0` |
| 原生 finding JSON / 去重 finding JSON | `1.0` / `finding-record-1.0`，原生记录与报告层去重记录不同 |
| vulnerability report | report schema `1.0` |

## 20. 配置接口

### 20.1 主流水线参数

除必需输入外，重要参数包括：

- `--semantic-task-budget`：每个 task + path + guide_index + target 在失败衰减前的执行预算，默认 1000；
- `--fuzz-iterations`：LibAFL 主循环迭代上限；
- `--fuzz-timeout`：整个 fuzzer 进程墙钟秒数；
- `--semantic-runtime-trace`：逐 hit 语义 trace；
- `--semantic-debug-artifacts`：详细 STG sidecar、DOT 和 task state；
- `--fb-max-cycles`、`--fb-stale-threshold`、`--fb-state-trace`、`--fb-state-trace-file`；
- `--build-only`、`--report-only`、`--skip-report`；
- 报告 provider、输出目录、批量 target-corpus 回放限制/超时、跳过该批量回放和最小化选项。

`semantist-harness` 的调用形式为 `semantist-harness <pou-name> --ir <target.ll> --stg-model <stg-model.json> [--out harness.c] [--seed-out seed] [--verify fixed_harness.c]`；正常使用由 `build_target.sh` 调用。

### 20.2 重要环境变量

| 变量 | 含义 |
| --- | --- |
| `SEMANTIST_ARTIFACT_DIR` | artifact root |
| `SEMANTIST_LLVM_BIN` | LLVM 21 工具目录 |
| `SEMANTIST_ARRAY_BOUNDS_SANITIZER` | IR bounds-checking 开关，默认开 |
| `SEMANTIST_MEMORY_BOUNDS_CHECKS` | SysMem checked rewrite 开关，默认开 |
| `SEMANTIST_STRIP_GLOBAL_CTORS` | 是否移除 global ctors，默认关 |
| `SEMANTIST_SEMANTIC_TRACE_FILE` | semantic coverage 事件文件 |
| `SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE` | 逐 hit trace 文件 |
| `SEMANTIST_SEMANTIC_TRACE_RUNTIME` | 开启逐 hit runtime trace |
| `SEMANTIST_FB_MAX_CYCLES` | engine 统一的 FB 周期上限，默认 256、最高 10000；0 或非法值回到默认 |
| `SEMANTIST_FB_STALE_THRESHOLD` | 连续不变周期停止阈值，0 禁用 |
| `SEMANTIST_FB_STATE_TRACE[_FILE]` | FB 状态 trace 开关/路径 |
| `SEMANTIST_SEMANTIC_STATE_DETAIL` | 输出详细任务状态 |
| `SEMANTIST_TIMEOUT_MS` | Rust engine 单次目标执行超时，默认 1000 毫秒 |
| `SEMANTIST_RNG_SEED` | Rust engine RNG 种子；省略时使用当前时间来源，实验 runner 显式注入配对种子 |
| `SEMANTIST_ABLATION` | Rust engine 消融配置，默认 `full`；engine `--ablation` 可覆盖 |
| `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` | OpenAI-compatible 报告调用的直接配置；三者均须有效 |
| `SEMANTIST_LLM_BASE_URL/MODEL` | base URL/model 的默认值来源，可被对应 `LLM_*` 变量覆盖 |
| `SEMANTIST_LLM_RETRY_DELAYS` | 可选报告 LLM 重试间隔，默认 `5,15` 秒 |

内部构建还使用 `SEMANTIST_STG_DIR`、`SEMANTIST_STG_CODEGEN_MAP`、`SEMANTIST_SEMANTIC_RUNTIME_IDS`、library/stubs/manifest 路径等变量；通常应由主流水线设置，而不是 benchmark 脚本手工拼接。

### 20.3 批量实验入口与计量边界

比较实验推荐在仓库根目录使用 `python run_experiment.py`。它只把预设参数交给现有 runner，不负责 Git 提交、远程部署或自动解释结果。当前默认 manifest 有 90 个目标；`main` 会读取 manifest 的全部目标，而不是在程序中写死 90。

| 预设 | 默认工具 | 目标 | 每工具每目标的预算与重复 | 默认 CPU ID |
| --- | --- | --- | --- | --- |
| `main` | SemantiST、AFL++、ICSQuartz | 当前 manifest 全部目标 | 300 秒 × 5 次 | `1-8` |
| `smoke` | 同上 | `oscat_basic_charname`、`icsfuzz_bf_mcpy_1`、`scan_cycle_aircraft_oobw_4` | 20 秒 × 1 次 | `1-2` |

常用覆盖参数为 `--run-id`、`--tools`、可重复的 `--target-id`、`--fuzz-time`、`--fuzz-trials` 和 `--cpus`。显式目标列表替换预设目标；`--run-id`/`--experiment-id`、`--fuzz-trials`/`--trials` 是别名。`--help` 展示常用接口，`--help-all` 展示高级参数。CPU ID 必须改为宿主机实际可用的编号；runner 将并发 trial 绑定到不同 CPU，`--serial` 可改为串行。

实验默认关闭文件级 FB state trace，最大周期 256、stale threshold 0、单次执行超时 1000 毫秒；共享内存周期/状态反馈仍启用。非 `full` 消融必须是独立的 SemantiST 单工具实验。原有 comparison、single-tool 和 ablation 脚本继续兼容，常用步骤见 [服务器操作手册](server_experiment_runbook_zh.md)。

每次使用新 run ID，现有实验目录即使为空也不复用；旧的 `--resume`、`--overwrite` 和 `--reuse-preprocessing` 已移除。`--preflight-only` 只产生环境、目标、支持矩阵和调度记录，不构建、不在线 fuzz，也不导出 `metrics.csv`。正常运行先预处理，再按独立 trial 执行在线预算；严格校验通过后导出逐 trial 的 `metrics.csv`/`metrics.jsonl`。失败保留原始制品并返回非零，不把结果目录存在视为验收成功。预设和最终解析出的目标/预算分别保存在环境及实验清单中。

共同计量合同为 `semantist.independent-tool-metrics/1`：

- 在线预算从宿主机启动在线命令计时，含工具启动、初始输入执行和 SemantiST 语义初始候选生成；离线编译不在该预算内。
- `total_executions` 是工具原生报告的 testcase 执行次数，不是 PLC 扫描周期数，也不从吞吐率反推；末次报告可能不是精确停止计数，需保留 `total_executions_window` 的来源边界。
- 有效 run 必须受支持、完成预算、没有失败原因、实际墙钟达到预算，且原生执行计数为正整数；离线重放不能修复原生无效 run。
- 公共 crash oracle 为 ASan、UBSan 或目标信号；timeout、OOM、语义 objective 与公共 crash 分开。首次 crash 的时间来自 runner 首次观察到制品，执行序号来自原生计数；预算后才观测到的发现不能混入预算内结果。
- CSV 是逐 trial 数据导出，不是漏洞根因去重、显著性检验或结论。默认不做公共 replay、coverage 和聚合；结束后可手动运行这些分析，高级显式开关仍保留。

计量细节及三工具的执行模型差异以 [实验规范](experiment_standard.md) 为准。

## 21. 可复现性与确定性边界

工具为可复现性提供：

- 固定 RuSTy revision、声明 Rust/LLVM 版本，并记录实际镜像和环境；
- 输入相对路径和 SHA-256；
- stable semantic target ID；
- 确定性排序、Tarjan SCC 和 bounded path enumeration；
- 内容哈希 seed 去重；
- JSON schema 版本；
- 运行事件、任务状态、reproducer 和回放结果。

但完整 fuzzing 仍不是位级确定过程。随机变异、概率调度、forkserver 时序、执行时间 feedback、外层超时和宿主资源竞争都会影响 corpus 演化。因此 benchmark 应固定镜像、CPU/内存、单次时长或迭代预算、任务预算、trace 开关和重复次数，并保留每次独立 run 目录。调试 trace 会显著改变 I/O 特征，不应在对比组中只对一组开启。

## 22. 当前实现边界与论文表述注意事项

以下限制直接来自源码，应在后续论文中如实界定：

1. **端到端目标范围。** STG 可表示多类 POU，但自动 Harness 只支持 FUNCTION 与 FUNCTION_BLOCK。
2. **Harness ABI 绑定 RuSTy 的代码生成约定。** 布局读取依赖 RuSTy 在 FB 入口为每个成员生成命名 GEP、在函数入口把参数存入同名 alloca；RuSTy 改变这些约定时生成器会报错而不是猜测。不透明成员（嵌套 FB、STRUCT）只按字节 fuzz，不展开其内部字段。
3. **configuration/constant input 不由 Harness 强制不可写。** 它们在 planner 和 seed 作用域中被区别对待，但名称匹配的 testcase 仍可赋值。
4. **FB 状态是有限观察。** 默认抽象反馈不纳入指针；可选精确快照也只观察地址或有限 backing bytes，不是任意 pointee heap graph。state hash 相同不证明完整状态相同。
5. **周期语义依赖记录顺序。** 手工 seed 应保证相同 cycle ID 连续；工具生成/变异路径会规范化。
6. **调用语义有限。** 调用 effects 可按签名保守建模，但当前不是完整跨过程函数体求解。
7. **bounded path 不是可达性证明。** SCC + BFS 给出有限候选引导路径，不展开无限循环，也不证明路径可满足。
8. **表达式求解是有界启发式。** 深度限制和支持运算集合明确，不等同于 SMT 或符号执行。
9. **IR mapping 允许 unmapped。** lowering、优化、元数据缺失或 IR 形状不足都会使目标无探针；诊断必须纳入覆盖解释。
10. **数组 violation 的 IR 推导有限。** 依赖保留下来的 GEP/type 形状，使用扁平元素计数，不能泛化为所有数组/指针布局。
11. **conservative 目标不是漏洞证据。** 只有 exact semantic violation/property 或确定性运行时故障进入 objective。
12. **semantic hit 不等于安全影响。** reach、branch、cycle 等只是覆盖/引导信号；报告还需运行时证据与回放。
13. **TimeFeedback 也可收种子。** corpus 增长不能全部归因于语义或边覆盖 novelty。
14. **输入去重是有界单进程过滤。** 最多 65,536 个 u64 指纹，FIFO 淘汰后可重新执行，且存在碰撞可能；不能描述为永久或跨运行的精确缓存。
15. **property violation 目前是模型预留能力。** 当前没有通用用户属性语言前端。
16. **报告 trace 归属有边界。** 附加式 state/runtime trace 未提供严格的每-testcase 事务隔离时，不应过度解释某条 trace 与某个 finding 的唯一对应。
17. **LLM 不参与发现判定。** 不能把报告语言模型描述为 fuzzing oracle 或漏洞检测器。

## 23. 值得论文重点阐述的实现机制

本节列出从实现看具有研究表达价值的机制。它们是“候选贡献点/需要实验验证的设计”，不是本文替论文作出的新颖性或优越性结论。

### 23.1 编译器语义贯穿动态 fuzzing

STG 不是终点：stable ID 经 runtime ID、codegen map、RuSTy LLVM metadata、IR probe、shared-memory bitmap 回到 scheduler 和 report。该闭环使源级目标可以被动态观测，并避免仅凭 LLVM CFG 猜测 ST 的 `CASE`、扫描周期或 hazard 语义。

论文可具体说明 stable identity、one-to-many/unmapped 映射、边 probe block 和诊断机制，并通过 mapping coverage 实验说明信息在 lowering/IR 阶段的保留率。

### 23.2 reach/violation 与可信度分层

把危险位置可达和危险条件成立拆成两个目标，同时传播 Exact/Conservative/Opaque，是连接静态语义和动态证据的关键。即使 violation 无法插桩，reach 仍可引导；同时 conservative hit 不被误当 objective。

适合验证的问题包括：reach 引导对最终 violation 的贡献、exact/unmapped 比例、关闭 violation condition 后的变化以及误报边界。

### 23.3 PLC 扫描周期的一等建模

FUNCTION_BLOCK 的 cycle entry/exit、persistent-state temporal edge、多周期 task requirement、周期 seed、状态摘要和周期变异形成一个完整链路。它不是简单重复执行相同输入，而是允许前置周期建立状态、末周期触发目标。

适合对比单周期 Harness、多周期但无状态引导、完整 temporal task 三种设置，并分别观察 stateful targets、发现时间和 seed cycle length。

### 23.4 任务—种子联合排序

任务优先级同时考虑 control、dependency、state、temporal 和 hazard effort，以及 seed affinity、state progress、gain 与失败衰减；每任务维护 top-4 seed 缓存并保留探索概率。它比只给 testcase 一个全局 energy 更接近“哪个 seed 适合推进哪个语义目标”。

论文应给出源码中的公式、状态机、预算和概率，并以消融分别移除任务排序、seed affinity、失败 deferred/reactivation 和 exploration。

### 23.5 typed、goal-directed、cycle-aware mutation

mutation 既保持文本 schema 合法，又利用 type bounds、guard expression、def-use/transfer、hazard predicate 和 temporal requirement。尤其 denominator 定向置零、overflow boundary、索引边界与最后周期 goal 注入，均能和 IR 动态 violation 条件形成可解释对应。

应通过结构化变异对 byte-level 变异、typed-only 对 semantic-goal、single-cycle 对 cycle-aware 的独立消融确认贡献。

### 23.6 intake 的语义保守原则

非破坏转换、review-required/unsupported 分层、显式 provider、所选 POU 的 native link 检查和“不自动生成未知默认实现”提供了可审计的 intake 过程。使用者仍需审核转换、确认环境模型并执行代表输入；当前生产入口不会自动强制检查 intake verification，也没有工具层面的完整语义等价证明。对于真实工业库，这些审核决定 benchmark 能在什么假设下代表原库语义。

可报告自动转换率、人工审核项、依赖闭包、链接成功率和兼容 shim 覆盖，但需要把转换工程能力与 fuzzing 核心算法贡献分开讨论。

## 24. 源码导航与事实对应

后续维护本文时，应优先检查以下实现文件：

| 主题 | 源码位置 |
| --- | --- |
| 主流水线与 CLI | `fuzzer/pipeline/core.py` |
| 实验预设、运行与严格校验 | `run_experiment.py`、`experiments/comparison/run_full_comparison.py`、`experiments/comparison/analysis/validate_complete.py` |
| 运行时消融开关 | `fuzzer/engine/src/config.rs`、`experiments/comparison/ablations.py` |
| 公共实验计量与逐 trial 导出 | `experiments/comparison/lib/metrics.py`、`experiments/comparison/analysis/compare_tools.py` |
| artifact 路径 | `fuzzer/runtime/paths.py` |
| 目标 ABI（STG 类型 + IR 布局） | `compiler/harness/abi.py`、`compiler/harness/llvm_ir.py` |
| Harness/seed/FB 状态 | `compiler/harness/generator.py` |
| 方言规则与 intake | `compiler/parser/st-to-rusty-converter/scripts/` |
| compatibility manifest/native provider | `compiler/toolchain/compatibility/` |
| ST 编译与 IR 准备 | `compiler/instrumentation/compile.py`、`compiler/scripts/` |
| RuSTy 语义 metadata | `compiler/toolchain/rusty-patches/0001-semantist-semantic-metadata.patch` |
| STG 编译与提取 | `compiler/stg/src/compiler.rs`、`extract.rs` |
| STG schema | `compiler/stg/src/model.rs` |
| stable ID | `compiler/stg/src/stable_id.rs` |
| STG 校验 | `compiler/stg/src/validate.rs` |
| runtime ID/codegen map | `compiler/stg/src/instrumentation.rs` |
| LLVM IR 插桩 | `compiler/stg/src/ir.rs` |
| 语义 runtime | `compiler/instrumentation/runtime/semantic_coverage.c` |
| 语义任务规划 | `fuzzer/semantic/planning.py` |
| Rust 任务 runtime/profile/ranking | `fuzzer/semantic/runtime.rs` |
| observers/metadata/io | `fuzzer/semantic/observers.rs`、`metadata.rs`、`io.rs` |
| Python corpus schema/校验 | `fuzzer/corpus/store.py` |
| STG bounded 初始种子生成 | `fuzzer/corpus/generation.rs` |
| target-aware scheduler | `fuzzer/scheduler/engine.rs` |
| 预算与任务状态输出 | `fuzzer/stages/semantic_budget.rs` |
| 结构化/语义/周期变异 | `fuzzer/engine/src/mutations.rs` |
| engine 组装、feedback/objective | `fuzzer/engine/src/main.rs` |
| 原生 finding recorder / 报告层去重 | `fuzzer/engine/src/mutations.rs`、`fuzzer/semantic/metadata.rs` / `fuzzer/reporting/findings.py` |
| 回放、去重和报告 | `fuzzer/reporting/report.py` |
| 确定性最小化 | `fuzzer/reporting/minimize.py` |
| LLM 摘要边界 | `fuzzer/reporting/llm.py` |

## 25. 总结

SemantiST 的完整实现可以概括为四个相互约束的层次：

1. **输入真实性层**：非破坏方言归一化、显式依赖 provider 和最终链接验证；
2. **编译器语义层**：源级 STG、稳定目标、类型/控制/数据/状态/危险模型，以及到真实 LLVM codegen 的映射；
3. **动态搜索层**：双 bitmap、语义任务与 seed 联合排名、typed goal mutation、多扫描周期执行和状态 novelty；
4. **证据层**：exact violation 与运行时故障 objective、结构化 reproducer、回放、去重、最小化和确定性报告。

理解工具时不能只看其中一层。例如 STG target 数不是动态映射数，semantic hit 不是自动漏洞，FB state hash 不是完整状态证明，归一化成功也必须经过 native link 验证。反过来，正是这些显式边界和跨层 ID/证据链，使 SemantiST 的实现能够被拆解、审计和通过后续实验逐项验证。
