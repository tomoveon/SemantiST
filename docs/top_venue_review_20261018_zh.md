# SemantiST 审稿式评审与顶会改进路线（2026-10-18）

> 评审视角：工控安全方向教授 / 安全顶会审稿人。
> 评审对象：HEAD `00f71fb`（工作区干净）的源码，以及 `artifacts/experiments/` 下两次正式实验
> （`comparison_3tools_90targets_300s_5trials`：SemantiST 450 run；`baselines_postrun_20261012_203250`：AFL++ 与 ICSQuartz 共 900 run）。
> 本文所有数字都可以从这两份实验产物复算，方法见附录 B。凡标注“待 CODESYS 确认”的结论，都还没有在真实 PLC 运行时上验证。

---

## 0. 结论先行

**0.1 现状判断。** 以当前代码和实验结果，我不建议投稿任何 CCF-A/B 安全会议。问题不在算法复杂度不够，而在两点：

1. **实验结果目前不能支撑任何结论。** 三个工具在 90 个目标上报告“发现崩溃”的目标数分别为 SemantiST 51、ICSQuartz 48、AFL++ 38，表面上 SemantiST 第一。我对 61 个 OSCAT 目标的崩溃逐一重放、符号化并分类：原始计数为 SemantiST 25 / AFL++ 24 / ICSQuartz 29，其中**可能是真实程序缺陷的只有 5 / 4 / 5 个**。其余约 80% 来自五类伪影：RuSTy 标准库 panic、RuSTy 代码生成或时间类型语义、我们自己方言转换引入的语义改变、SemantiST harness 的内存布局错误，以及字节级输入产生的非法 BOOL 值。三个工具各自的“独有发现”**几乎全部是伪影**。
2. **主线缺失。** 现在的叙事是“STG + 定向调度 + 结构化变异 + 多扫描周期”四个机制的堆叠。每个机制都有直接前作：定向灰盒 fuzzing 有 AFLGo/Beacon 一系，扫描周期有 ICSQuartz 的自适应扫描与 stale-cycle 变异，类型化输入有 structure-aware fuzzing。目前没有一个“前作做不到、我们做到了”的问题陈述。

**0.2 好消息。** 这些伪影本身，恰好构成一个有顶会潜力、又与 SemantiST 已有能力高度契合的故事：**PLC fuzzing 的语义保真度鸿沟（fidelity gap）**。在“开源编译器重编译 ST + ASan/崩溃 oracle”的范式下，fuzzer 里的崩溃不等于 PLC 上的故障，PLC 上的故障也不等于 fuzzer 里的崩溃。证据有三重：

- 本次数据：约 80% 的“崩溃”在语义上不成立（第 2 节）。
- ICSQuartz（NDSS'25）论文附录 B 自述：RuSTy 标准库字符串函数对非 ASCII 输入 panic，是其 OSCAT 测试中崩溃的来源之一。它的讨论部分还明确把“在真实 PLC 上自动交叉验证”列为未来工作。
- 你本人在 CODESYS Control Win V3 上的手工复现（`docs/notes/oscat漏洞报告.txt`）：INC/INC2 的 `MOD 0` 在真实运行时**不崩溃**，而是静默返回常量，导致调用方死循环、看门狗超时、PLC 停机；在 RuSTy/x86 harness 里同一输入则是 SIGFPE 崩溃。

**0.3 推荐主线。** 从“更聪明的 fuzzer”转向“**更忠实的 fuzzer**”：SemantiST 用编译器语义构造对目标 PLC 运行时保真的执行与 oracle，并在真实 PLC 运行时上自动交叉验证每一个报告。不和 ICSQuartz 比吞吐，比“报告出来的每个问题在真实 PLC 上是否成立、是否有安全后果”。

**0.4 必须先做（P0，约 2–3 周）。** 修复 harness 的两处致命布局缺陷和方言转换的一处语义缺陷，修复实验基础设施的若干缺陷，建立统一的重放与分类流水线（第 3.2 节、第 2.3 节）。在此之前，任何新的大规模实验都是浪费机时。

**0.5 发表策略。**
- **顶会（NDSS / USENIX Security / CCS / S&P）**：需要“测量研究 + 系统 + 真实 PLC 自动交叉验证 + 若干新漏洞（最好有 CVE）”。
- **保底 CCF-B（ACSAC / ESORICS / RAID / DSN）**：同一故事的缩小版，即一个 CODESYS 语义 profile、在 OSCAT 与注入基准上的完整分类评估、若干已在 CODESYS 上复现的真实缺陷（第 7 节）。

---

## 1. 审查范围与方法

| 项目 | 内容 |
| --- | --- |
| 源码 | 重点通读 `compiler/parser/st.py`、`compiler/harness/generator.py`、`compiler/stg/src/model.rs`、`compiler/instrumentation/runtime/semantic_coverage.c`、`compiler/parser/st-to-rusty-converter/scripts/dialect_rules.py`、`fuzzer/engine/src/main.rs`、`fuzzer/semantic/observers.rs`，并对照 `docs/` 中的算法说明及 2026-10-08 的历史审查与验收记录。`extract.rs`（3291 行）与 `mutations.rs`（2637 行）只做了关键路径抽查，对其算法的评价主要依据设计文档。 |
| 实验数据 | 两份 `metrics.csv`（1350 行）、各 run 的 `run-result.json`、sanitizer 日志、崩溃输入、预处理产物（含 LLVM IR）。 |
| 重放分类 | 对每个（工具，目标，trial）取前 3 个崩溃输入。AFL++ 的输入用其预处理目录中的 ASan 版 `fuzz_target` 重放。ICSQuartz 在 OSCAT/ICSFuzz 上与 AFL++ 共用同一 benchmark `harness.c` 的字节布局，因此用同一 ASan 二进制重放；扫描周期输入含多周期结构，没有重放。SemantiST 直接解析 sanitizer 日志，并用 `addr2line` 对其未裁剪的 `fuzz_target` 做栈帧符号化。对可疑类别再查 LLVM IR 和 ST 源码确认根因。 |
| 局限 | 未在 CODESYS 上复核；每个 trial 只抽前 3 个崩溃，后续的真实缺陷可能被先到的 panic 遮蔽（这本身就是问题之一，见 2.4）；标为“可能真实”的条目仍需真实运行时确认。 |

---

## 2. 实验结果审查（最关键）

### 2.1 原始结果：直接阅读会得出错误结论

| 套件（目标数） | SemantiST | AFL++ | ICSQuartz |
| --- | ---: | ---: | ---: |
| OSCAT Basic（61） | 25 | 24 | 29 |
| ICSFuzz 血缘（17） | 17 | 7 | 11 |
| 扫描周期（12） | 9 | 7 | 8 |
| **合计发现崩溃的目标（90）** | **51** | **38** | **48** |
| 有效 run | 365/450 | 375/450 | 376/450 |
| 吞吐中位数，exec/s（OSCAT 上未崩溃的 run） | 331 | 221 | 335,419 |

另有两个全局事实：**1350 个 run 的 `first_crash_time_seconds` 全部为 null，`replay_confirmed_crash_found` 没有一个为 True**。也就是说，fuzzing 论文最核心的三项指标（首次发现时间、经确认的缺陷、去重后的缺陷数）目前都不存在。

### 2.2 OSCAT 61 个目标的重放分类

分类定义（每类都附有一个已经核实的具体证据）：

| 标签 | 含义 | 已核实的证据 |
| --- | --- | --- |
| **T1** | RuSTy 标准库 UTF-8 panic | 重放 AFL++ 在 CLEAN 上的崩溃输入 `\xd7\n`，得到 `panicked at libs/stdlib/src/string_functions.rs:83:49: called Result::unwrap() on an Err value: Utf8Error`。CODESYS 的 STRING 是单字节串，任意字节都合法。 |
| **T2** | RuSTy 标准库长度或日期 panic | `LEFT_EXT__STRING: Requested substring length exceeds string length`；`DATE_AND_TIME_TO_DATE: Out of range, cannot create DATE`。CODESYS 对应函数的行为待确认。 |
| **T3** | RuSTy 代码生成 | FSTRING_TO_BYTE/TRIM1/TRIME 中，短字面量（`utf08_literal_N`，2–3 字节）被按整串长度 `memcpy`，产生 global-buffer-overflow。 |
| **T4** | 方言转换改变语义 | `dialect_rules.py` 第 4 条规则用正则把 `ADR(` 换成 `REF(`。FINDB_NUM 中 `pt := REF(str) + LEN(str) - 1` 在 IR 中变成 `getelementptr [81 x i8], ptr %str, i32 LEN`，即按 STRING 元素大小（81 字节）缩放；而 CODESYS 的 `ADR()+n` 是字节偏移。FINDB_NUM/FINDB_NONUM 的 stack-buffer-overflow 由此产生。 |
| **T5** | 时间类型位宽与取值域 | RuSTy 的 TIME 是 64 位纳秒，CODESYS 是 32 位毫秒。FILTER_I 的 FPE 输入为 `T=2191000000000`、`T=1.0e308` 等，超出 CODESYS 取值域；`TIME_TO_DINT(T)` 截断后可能为 0。（待 CODESYS 确认） |
| **I1** | 非规范 BOOL | AFL++ 在 PARSET 上的崩溃输入中 A0 字节为 `0x0a`。合法 BOOL 下 `set0 ≤ 3`，`X[set0, …]` 不会越界。 |
| **H1** | SemantiST FB 实例布局错误 | 嵌套 FB 被当成 8 字节指针。CTRL_PID 的 IR 类型大小为 **264 字节**（内嵌 `FT_PIDWL` 160 字节、`CTRL_OUT` 40 字节），SemantiST 手写的 C 结构体只有 **80 字节**；在构造函数（`CTRL_PID__ctor`）或第一次调用时即发生栈溢出。 |
| **H2** | SemantiST 数组长度错误 | `VAR_GLOBAL CONSTANT` 不被解析，`ARRAY[0..ArraySize]` 退化为 1 个元素（见 3.2 P0-2）。 |
| **C1** | 指针 + 长度契约违反 | `_BUFFER_INSERT`、`_STRING_TO_BUFFER`、`BUFFER_COMP` 的指针与 SIZE 被独立变异。审稿人会视为 API 误用，除非能证明合法调用下也会越界（如你笔记中 `_STRING_TO_BUFFER` 的 SIZE=0 差一错误）。 |
| **U** | 待定 | DT_TO_STRF 在编译器临时变量 `__DT_TO_STRF192` 上栈溢出，可能是 RuSTy 字符串临时变量尺寸问题，需要 CODESYS 对照。 |
| **G** | 可能真实（仍需 CODESYS 确认行为） | INC、INC2、SIGNAL、SCHEDULER_2 的整数 DIV/MOD 0；WEEKDAY_TO_STRING 的负索引越界读，与 CVE-2024-6876（MONTH_TO_STRING）同型。 |

按工具汇总（OSCAT 61 个目标；逐目标明细见附录 A）：

| 类别 | SemantiST | AFL++ | ICSQuartz |
| --- | ---: | ---: | ---: |
| T1–T5 工具链 / 转换 / 类型语义 | 9 | 18 | 22 |
| I1 非法输入编码 | 0 | 1 | 1 |
| H1 自身 harness 布局 | 8 | 0 | 0 |
| C1 契约违反 | 3 | 0\* | 0\* |
| U 待定 | 0 | 1 | 1 |
| **G 可能真实** | **5** | **4**† | **5** |
| 原始合计 | 25 | 24 | 29 |

\* AFL++ 与 ICSQuartz 在 `_BUFFER_INSERT`、`_STRING_TO_BUFFER` 上先命中 UTF-8 panic，因此计入 T（ICSQuartz 有少量样本属于 C1 型全局溢出）。
† AFL++ 在 SCHEDULER_2 上因初始种子即崩溃而拒绝启动（见 2.3 第 1 条）。

**结论：** 原始排名“ICSQuartz 29 > SemantiST 25 > AFL++ 24”在分类后变为“5 / 5 / 4，基本打平”。三个工具真正共同发现的只有 INC、INC2、SIGNAL、WEEKDAY_TO_STRING 这几个浅层缺陷，而且其中的除零在 CODESYS 上的表现与 fuzzer 中完全不同。

**另外两个套件：**

- **ICSFuzz 血缘（17）**：11 个 `bf_*` 注入缺陷三个工具都能触发（AFL++ 有 4 个因种子即崩溃未启动）。6 个 `oob_*` 只有 SemantiST 跑起来，两个 baseline 都因链接失败未能运行（2.3 第 2 条），**不构成比较**。
- **扫描周期（12）**：这是唯一可能显示 SemantiST 优势的套件（9 vs 7 vs 8）。按全部 sanitizer 日志，SemantiST 的 9 个目标分两类：
  - 4 个（aircraft_oobw_4、anaerobic_oobr_1、chemical_oobr_1、smart_oobr_1）的崩溃位置在 harness 的 `f0/f1/f2_storage` 上，属 H2。例如 aircraft_oobw_4 的 `In_Tmp` 实为 4 个 LREAL，harness 只分配了 `double f2_storage[1]`，目标在正常访问 `In_Tmp[1..3]` 时即越界。
  - 5 个与注入缺陷一致。aircraft_oobw_5、anaerobic_oobr_2、chemical_oobw_1 是 fuzz 可控长度的 `MemCpy`/`SysMemCpy`，ASan 先报的是 `memcpy-param-overlap` 而不是越界；anaerobic_oobr_2 已符号化确认调用链为目标 FB → `__SEMANTIST_MEMCPY_CHECKED`（`benchmarks/external/compatibility/codesys-memory/runtime.c:50`），另两个签名相同。anaerobic_oobw_3、smart_oobw_1 是 heap-buffer-overflow，未符号化。这 5 个目标同样声明了 `VAR_GLOBAL CONSTANT`，修复 H2 后需复核。

  扣除 H2 后 SemantiST 可信的约为 5 个，**并不领先**。ICSQuartz 的 60 个 run 中有 44 个为 runner_error，baseline 的 7/8 也未经重放确认。**两边都需要修复后重跑**，现有数据不可引用。

### 2.3 实验基础设施缺陷

| # | 问题 | 影响 | 证据 |
| --- | --- | --- | --- |
| 1 | 初始种子即崩溃时 AFL++ 拒绝启动 | 9 个目标 × 5 = 45 个 run 记为 `online_crashed`、`crash_found=False`，实际上是“第 0 次执行即崩溃” | stdout 中的 `PROGRAM ABORT : We need at least one valid input seed that does not crash!` |
| 2 | `icsfuzz_oob_*` 在 baseline 中链接失败 | AFL++、ICSQuartz 各 30 个 run 失效；同时说明 SemantiST 目标带 local-bounds（UBSan）检测而 baseline 没有，与《实验标准》中“共同使用 ASan、不为部分工具单独启用 UBSan”相矛盾 | `undefined reference to __ubsan_handle_local_out_of_bounds_abort`，来源是预处理中的 `opt -passes=function(bounds-checking<rt-abort>)` |
| 3 | ICSQuartz 扫描周期 run 大量无效 | 60 个 run 中 44 个为 `icsquartz_online_logs_incomplete` | `metrics.csv` |
| 4 | SemantiST 启动输入全为 objective 时直接退出 | 85 个 run 被判无效；H1 的 8 个 FB 目标**实际上没有被 fuzz 过** | `fuzzer/engine/src/main.rs:262-277` |
| 5 | 没有首次发现时间、没有重放确认、没有去重 | fuzzing 论文的核心指标全部缺失；SemantiST 在 INC 单个 trial 中保存了 2238 个 finding 文件 | `first_crash_time_source=unavailable_without_online_observation` |
| 6 | 比较条件不对等 | SemantiST 与 baseline 分两次 campaign、在不同时间段运行（CPU 池同为 `1-8`，但不是交错调度）；种子不一致（SemantiST 用手写结构化 seed，AFL++ 用 adapter 字节 seed，ICSQuartz 用自带生成器）；ICSQuartz 在 OSCAT 的 FB 上没有开启扫描周期模式（`execution_model=single_cycle`），而多数 FB 逻辑至少需要 2 个周期 | `environment.json`、`metrics.csv` |
| 7 | ICSQuartz checkout 带未提交修改 | 审稿人会要求公开 diff 并论证不影响其搜索能力 | `environment.json`：`evaluate-all.sh`、`run_experiment.py`、`scripts/calibrate-codesys.sh` 为 dirty |
| 8 | 吞吐异常 | AFL++ 在仅 35 条边的 BIT_COUNT 上只有 162 exec/s，正常的 forkserver + ASan 应在数千量级；审稿人会认为 baseline 配置错误。SemantiST 与 ICSQuartz 相差三个数量级 | AFL++ 的 `fuzzer_stats` |
| 9 | 语义 objective 的元数据不可追溯 | solution 的 `metadata.map` 为空，无法从结果判断触发的是哪个语义目标 | `target-corpus/.*.metadata` |

### 2.4 数据里真正有价值的信号

1. **类型化输入天然避开 T1、I1 类伪影。** SemantiST 在 13 个字符串目标和 PARSET 上“漏报”，从保真度角度看恰恰是正确行为。这是新主线的正面证据，但需要在 CODESYS 上确认这些输入在真实运行时确实无害。
2. **伪影会遮蔽真实缺陷。** 字节级工具几乎每次都先撞上 UTF-8 panic；一旦某个目标被判为“已发现”，其上可能存在的真实缺陷就不会再被统计。
3. **SemantiST 已能报告不崩溃的 exact 语义违规**（例如 CONTROL_SET1、COUNT_DR、ALARM_2、CALIBRATE 的 `target-corpus` 中都有 solution），但这些结果目前没有计入指标，也没有分类和验证。同时，全部 90 个目标中 `dangerous_conversion` 类 hazard 有 159 个为 conservative、只有 53 个为 exact，conservative 的不会成为 objective。因此你在 CODESYS 上手工确认的 REAL_TO_STRF（REAL_TO_DINT 溢出导致输出 `--695...`）这类缺陷，**当前工具检测不到**。
4. **你的 CODESYS 笔记本身就是论文的开篇例子**：同一个 `MOD 0`，在 fuzzer 中是崩溃，在真实 PLC 上是“静默错误、看门狗超时、停机”。

---

## 3. 源码审查

### 3.1 总体评价

**做得好的地方：**

- **语义来自编译器而不是正则。** `compiler/stg` 直接使用 RuSTy 的 AST 与类型信息，生成带稳定 ID、IR 映射和建模状态（exact / conservative / opaque）的 STG，是扎实的基础设施，也是新主线可以直接复用的核心资产。
- **输入可读、可重放。** 输入是类型化的 `NAME,TYPE,VALUE` 加扫描周期序列，便于重放、最小化和写进报告。
- **hazard 的“到达”与“违规”分开建模**，思路正确。
- **可复现性意识强。** 测试覆盖可观（Rust 72 项，Python 100 余项，另有 e2e），实验框架记录哈希、manifest 和溯源信息。

**结构性问题：**

1. **harness 不是由编译器生成的。** 现在是 Python 重新解析 ST 声明、自己推断 C 结构体布局。这是本次 SemantiST 伪影的总根源，而仓库里其实已经有一套正确做法：`benchmarks/generate_harness.py` 从 IR 推导实例大小，baseline 用的就是它。
2. **方言转换以正则替换为主**，其中至少一条规则会改变程序语义。
3. **执行路径慢。** forkserver、每次执行写文件、C 侧解析文本，中位吞吐约 331 exec/s。
4. **工程体量分配失衡**（见 3.5）。研究贡献的核心被大量外围代码淹没。

### 3.2 P0 正确性缺陷（任何新实验之前必须修复）

> **修复状态（2026-10-18，分支 `fix/p0-harness-fidelity`）**：P0-1、P0-2、P0-3、P0-5、P0-6 已修复；P0-4 只完成了底线做法，CODESYS 语义 profile 尚未开始。各条末尾的“状态”说明了改动和验证范围。修复前的实验数据仍然无效；重跑前须用当前源码重建实验镜像。

**P0-1　FB 实例布局错误（H1）**
- 位置：`compiler/parser/st.py:613-617`，未知类型（嵌套 FB、STRUCT、枚举、类型别名）一律返回 `TypeSpec("pointer", …)`；`compiler/harness/generator.py:115-135` 再把它生成为 `void *` 字段。
- 证据：CTRL_PID 的手写结构体为 80 字节，IR 中为 264 字节；8 个 OSCAT FB 在第一次执行时就报 ASan 栈溢出。枚举被当成 8 字节，还会让后续字段的偏移全部错位。
- 修复：harness 布局只能来自编译器。从 LLVM IR 的 FB 类型按 DataLayout 计算大小与各字段偏移（`benchmarks/generate_harness.py` 已经对 `PLC_PRG` 这样做了），或者在 `compiler/stg` 中导出 RuSTy 的类型布局；输入按偏移写入不透明字节区，不再手写 C struct。
- 防回归：构建时加 `_Static_assert(sizeof(实例) == IR 大小)`；新增回归测试，对 manifest 中全部 FB 断言 C 侧与 IR 侧大小一致。
- 状态：已修复。新增 `compiler/harness/abi.py`：逻辑类型取自 STG 模型，布局取自 IR（FB 成员 GEP、函数参数绑定），两者逐项交叉检查，不一致即报错。生成的 FB 结构体对实例大小和每个字段偏移都有 `_Static_assert`；`build_target.sh` 用本次构建重新生成 harness，要求与固定 fixture 逐字节一致。90 个基准的 `semantist_harness.c` 已重新生成，`check_benchmark_files.py --semantist-abi` 为 90/90。CTRL_PID、INTERLOCK 的基础种子不再在启动时溢出。原正则类型解析器已删除。

**P0-2　全局常量未解析（H2）**
- 位置：`compiler/parser/st.py:46` 的 `VAR_BLOCK_STARTS` 不含 `VAR_GLOBAL`。我实测 `integer_constants()` 对 `VAR_GLOBAL CONSTANT ... ArraySize : INT := 3;` 返回 `{}`，于是 `ARRAY[0..ArraySize]` 退化为 1 个元素。
- 影响：ICSQuartz 扫描周期套件中所有使用 `ArraySize` 的目标。
- 修复：同 P0-1 的根治方式，数组长度也取自编译器。即便只做临时修补，也要补上 `VAR_GLOBAL`，并让无法解析的界限直接报错，而不是静默回退为 1。
- 状态：已修复，随 P0-1 一并解决。数组界限、字符串容量和 `VAR_IN_OUT` 指向的类型都取自 RuSTy 的解析结果（`In_Tmp` 现为 4 个 LREAL）。界限未能解析时报错。AIRCRAFT_OOBW_4 的基础种子不再越界。

**P0-3　方言转换改变语义（T4）**
- 位置：`compiler/parser/st-to-rusty-converter/scripts/dialect_rules.py:43-47`（`ADR()`→`REF()`）。规则 6（`size : UINT`→`ULINT`）改变了参数宽度，同样需要论证。
- 修复：改为 AST 级转换。把 ADR 的算术改写为字节指针（`REF_TO BYTE`）或整数地址运算；并为每条规则建立“CODESYS 与 RuSTy 行为一致”的差分测试，测试表本身就能成为论文中的一张表。
- 状态：已修复已知实例，规则层面只做了部分修复。`oscat.st` 中 4 处 `REF(x) + n`（CODE、DWORD_TO_STRH、FINDB_NONUM、FINDB_NUM）和两个 FINDB 的 target.st 已改为先取引用、再对 `REF_TO BYTE` 做字节运算；已用 RuSTy 验证改写后生成 `getelementptr i8`。新增规则 27（标记为 review_required），用于今后转换时处理 `p := ADR(x) + n;`。注意：本版 RuSTy 中 `ADR(x)` 一旦参与算术就会导致编译器 panic，所以不能直接保留 ADR。差分测试表和规则 6 的论证尚未完成。

**P0-4　运行时语义 profile（T1、T2、T5）**
- 现状：RuSTy 标准库假设字符串是 UTF-8、LEFT/MID 越界时 panic、DATE 构造越界时 panic、TIME 为 64 位纳秒，都与 CODESYS 不一致。
- 底线做法：把 Rust panic 归为独立的 oracle 类别 `toolchain_panic`，从缺陷统计中排除。
- 贡献级做法：实现一个 CODESYS 保真的标准库 profile（字节串语义、长度钳制、时间位宽与取值域）。这是新主线的核心模块之一（5.4 节）。
- 状态：底线做法已完成。公共重放（`experiments/comparison/analysis/replay.py`）对所有工具都开启符号化，并把已确认的崩溃分为 `toolchain_panic`、`harness_fault`、`target_fault` 三类。指标新增 `replay_confirmed_target_fault_found` 等字段，只统计 `target_fault`。贡献级的 CODESYS profile 尚未开始。

**P0-5　启动即 objective 时退出**
- 位置：`fuzzer/engine/src/main.rs:262-277`。
- 修复：记录启动期的发现（首次发现时间为 0）后继续 fuzz，用非 objective 的 bootstrap 变体作为 corpus 起点；实验口径中把这类目标单列为“平凡崩溃目标”，而不是判为无效。
- 状态：已修复。启动输入全部被判为 objective 时，引擎记录 `startup_objectives_seeded_corpus` 事件，把基础种子放入队列后继续 fuzz。已用一个必崩目标验证：运行 15 秒、执行 97 次，所有崩溃去重为 1 条发现。实验口径中“平凡崩溃目标”的单列尚未实现。

**P0-6　可观测性**
- 每个发现记录相对 fuzzer 启动的单调时钟时间戳。
- solution 元数据写入触发的稳定 target ID 与 hazard 类型。
- 按（稳定 target ID，栈哈希）在线去重。
- 状态：已修复。运行时发现按（sanitizer 摘要 + 栈顶 3 帧的模块偏移）签名在线去重，每个签名只保存一份复现输入，并记录 `elapsed_seconds`（单调时钟）和执行次数。新达到的 exact 语义违规单独写成发现记录，带稳定 target ID、hazard 类型和建模状态。实验适配器把这些时间作为 `native_time_to_find_seconds` 传给指标。UBSan 改为打印栈。在 INC 上运行 60 秒：201 个 objective 去重为 1 个运行时发现加 1 个语义发现。

### 3.3 P1 算法与 oracle 层面

- **Hazard 模型过于保守。** conservative 的比例偏高，而 PLC 安全真正关心的几类故障，即类型转换溢出、整数回绕后流向数组索引或输出、除零在不同厂商运行时下的不同行为、无界循环导致看门狗超时，都需要 exact 且区分厂商的 oracle。
- **REAL 算术边界的安全含义存疑。** 把 IEEE ±inf 当作违规（CONTROL_SET1 的触发输入是 `-3.0e38`）缺乏安全意义。应只保留“结果流向输出、数组索引或控制条件”的情况；STG 的数据依赖信息正好可以做这个过滤。
- **定向调度的常数没有依据。** 优先级公式 `Gain/(1+Effort)·e^{-0.25f}`，以及 75/25、70/30、15%/20% 等比例都是手工常数，至今没有消融数据。论文里不要把它当作主要贡献；作为“把预算导向 PLC 故障点”的支撑机制即可，配一组小而干净的消融。
- **历史审查中的算法限制不必全部解决。** 2026-10-08 审查列出的限制（effort 不是距离、transfer 链未求解、路径是近似）在新主线下不再是核心问题，只需在论文中如实说明。

### 3.4 性能

- **现状：** LibAFL `ForkserverExecutor`，每次执行写输入文件，C 侧用 `strtod` 等函数解析文本，加上 ASan，中位吞吐 331 exec/s；ICSQuartz 为 3.4×10⁵ exec/s。
- **建议：**
  1. harness 改为 in-process 或 persistent 模式（LibAFL `InProcessExecutor` 或 `__AFL_LOOP`），FB 实例每次执行前从快照 `memcpy` 恢复，与 benchmark harness 的做法相同。
  2. 内部使用按 IR 偏移定长的二进制类型化编码，文本格式只用于交换、重放和报告。
  3. 遥测只在出现新颖事件时解析。
- **目标：** 达到 10⁴–10⁵ exec/s。否则凡是按 wall-clock 比较的实验，SemantiST 都无法成立；在新主线下吞吐不是卖点，但不能差三个数量级。
- **同时排查 AFL++ 的 162 exec/s。** 逐项排除 ASan 下的 fork 开销、容器、CPU 绑核冲突、harness 每次 `malloc(1 MB)` 等因素。baseline 配置错误是最常见的拒稿理由之一。

### 3.5 冗余与仓库整理

各部分代码量（行，含 Python/Rust/C/Shell）：

| 部分 | 行数 | 说明 |
| --- | ---: | --- |
| fuzzer 引擎（Rust：engine、semantic、scheduler、corpus、stages） | 8,930 | 研究贡献核心 |
| STG 编译器（Rust） | 6,438 | 研究贡献核心 |
| 实验框架 `experiments/` | 6,792 | 与引擎体量相当 |
| 测试 | 5,784 | — |
| harness、插桩、解析器、toolchain | 3,410 | 含两处 P0 缺陷 |
| 报告（含 LLM 润色） | 2,600 | 与研究贡献无关 |
| 方言转换器（AI Agent Skill） | 1,949 | — |
| 文档 `docs/*.md` | 4,482 | 大量免责式表述与历史记录 |

具体建议（先在 `paper/` 分支上整理，主干的删除留待你审阅后再做）：

1. **合并两套 harness 生成器**，以 `benchmarks/generate_harness.py` 的 IR 推导方式为基础，废弃 `compiler/harness/generator.py` 中手写结构体的部分（1289 行）。（已完成：两套 harness 现在都从 IR 推导布局，共用 `compiler/harness/llvm_ir.py`；正则类型解析器和固定 harness 的 telemetry 补丁路径均已删除。）
2. **清理仅转发参数的入口**：`experiments/comparison/run_complete_comparison.py`、`run_smoke_comparison.py`、`run_tool.py`；`experiments/ablations/` 下的 3 个 runner 和 `experiments/comparison/ablations.py`；`compiler/scripts/` 下三个 8 行的 Python wrapper。统一保留 `run_experiment.py`。
3. **处理方言转换器的定位。** `compiler/parser/st-to-rusty-converter/` 是 AI Agent Skill（含 `SKILL.md`、`agents/openai.yaml`），与“LLM 不参与 fuzzing”的主张放在同一个产品目录里，审稿人和 artifact 评审都会困惑。建议移出主仓，或改成普通的离线工具并补上测试；配合 P0-3 改为 AST 级转换。
4. **冻结报告模块。** `fuzzer/reporting/`（含 LLM 润色）在论文分支中冻结，不作为贡献宣传。
5. **删除无引用脚本**：`scripts/generate_stg_dot.py`（全仓无引用）。（已删除，与 STG 的 `--dot` 输出重复；同时删除了 `compiler/scripts/generate_harness.py` 转发入口。）
6. **拆分 Rust 引擎。** 引擎通过 `include!` 把 `fuzzer/semantic`、`scheduler`、`corpus`、`stages` 拼进同一个 crate，物理目录与模块边界不符；改为 lib crate 加 bin。
7. **重组文档。** 17 个文档 4.5k 行，大量“不能据此推导……”式的免责声明和历史审查。按“使用 / 设计 / 证据”三类重组，历史记录归档到 `docs/history/`。论文需要的是清楚的主张，而不是防御性的说明。
8. **补齐关键测试。** 新增两类测试：全部 FB 的 C 侧与 IR 侧大小一致性测试；方言转换和语义 profile 的“CODESYS vs RuSTy”差分测试。

---

## 4. 主线诊断：为什么现在讲不好故事

模拟三位审稿人的核心意见：

- **R1（fuzzing 方向）：** “这是在 PLC 上做 AFLGo/Beacon 式的定向灰盒 fuzzing。哪个组件是新的？优先级公式的常数没有依据。主比较只数崩溃，没有去重、没有分类、没有首次发现时间。吞吐比 ICSQuartz 低三个数量级。”
- **R2（工控安全方向）：** “报告的大多数问题要么是库 API 的误用，要么是 RuSTy 伪影。有哪个发现对真实 PLC 有意义？没有威胁模型：谁能控制这些 FB 的输入？”
- **R3（方法学）：** “没有真值、没有重放、两次 campaign 分开跑、种子和检测器不对等、ICSQuartz 没有开启扫描周期模式。不能接收。”

**根本原因：论文在回答“怎么做”之前，没有回答“为什么需要做”。**

一个顶会故事需要五个环节：

1. 一个社区以为已经解决、或者还没有意识到的问题；
2. 证明这个问题真实存在且规模很大的证据；
3. 一个能解释问题成因的洞察；
4. 一个建立在这个洞察上的系统；
5. 一个证明问题被解决、并带来真实影响的评估。

现在的 SemantiST 只有第 4 环的一部分。

---

## 5. 推荐主线：PLC fuzzing 的语义保真度

> 更新（同日）：综合 `docs/improvement/` 的材料后，主线调整为以“静默数值缺陷的后果判定”为技术核心，本节的保真度测量并入作为动机和 RQ1，见 `docs/improvement/综合建议（最优方向）.md`（已按外部审查意见修订）。注意：本文 §2.2 的伪影比例按（工具，目标）统计，引用时应改按独立根因和 CODESYS 确认状态重新统计。

### 5.1 核心论点

> 现有 ST fuzzer 把 PLC 程序当作 C 程序来测试：字节级输入、开源工具链的运行时语义、ASan 式的崩溃 oracle。我们发现这种语义鸿沟是**双向失真**的：它制造大量在真实 PLC 上并不存在的“崩溃”，同时漏掉真实 PLC 上会导致停机或静默错误输出、但在 fuzzer 中并不崩溃的故障。SemantiST 利用编译器语义，构造对目标运行时保真的执行环境与 PLC 故障 oracle，并自动在真实 PLC 运行时上交叉验证每一个报告。

标题候选：

- *Crashes That Don't Matter, Failures That Don't Crash: Semantics-Faithful Fuzzing of PLC Programs*
- *Mind the Runtime Gap: Fidelity-Aware Fuzzing of IEC 61131-3 Structured Text*

### 5.2 开篇示例（图 1）

用一张图展示鸿沟的两个方向：

| | fuzzer 中（RuSTy + ASan） | 真实 PLC 上（CODESYS Control Win V3） |
| --- | --- | --- |
| **INC**：`(X+D+M+1) MOD (M+1)`，`M = -1` | SIGFPE，判为崩溃 | 不崩溃，静默返回 0；调用方的 WHILE 永不退出，看门狗超时，PLC 进入 EXCEPTION/STOP |
| **CLEAN**：输入 `0xD7` | RuSTy 标准库 panic，判为崩溃 | 正常处理单字节字符（待确认） |

前者是“PLC 故障在 fuzzer 中只表现为一个毫无上下文的崩溃”，后者是“fuzzer 的崩溃在 PLC 上根本不存在”。

### 5.3 研究问题

- **RQ1（测量：误报）** 现有 ST fuzzing 流程报告的崩溃中，有多大比例在真实 PLC 运行时上不成立？成因如何分布？
  - 对象：ICSQuartz、AFL++、修复前的 SemantiST；OSCAT Basic/Building/Network 与 ICSQuartz 基准；在 CODESYS 上交叉验证。
- **RQ2（测量：漏报）** 有多少真实 PLC 故障（停机、看门狗超时、静默错误输出）在现有流程中不表现为崩溃？
- **RQ3（系统有效性）** SemantiST 的保真执行与 PLC 故障 oracle，在报告精度（经 CODESYS 确认的比例）、真实缺陷数和发现时间上，相对 baseline 如何？
- **RQ4（组件贡献）** 厂商语义 profile、类型化输入、扫描周期序列、STG 定向、oracle 各自贡献多少？（消融）
- **RQ5（实际影响）** 在真实库和工程中发现了哪些新缺陷？厂商确认和 CVE 情况如何？

### 5.4 系统设计：在现有代码上重构，而不是重写

| 模块 | 现有基础 | 需要新增或改造 |
| --- | --- | --- |
| ① 编译器派生的布局与类型 | STG 与 RuSTy 类型信息 | harness 布局全部来自编译器（P0-1、P0-2）；输入编码按 IR 偏移 |
| ② 厂商语义 profile（先做 CODESYS） | hazard 探针位置、compatibility profile 机制 | 覆盖：字符串字节语义；LEFT/MID/DELETE 等边界行为；TIME/DATE 位宽与取值域；整数除零与取模；REAL→整数转换；指针算术；以及 CODESYS“隐式检查 POU”（CheckBounds、CheckDivInt/DInt/Real、CheckRange\*、CheckPointer 等）是否启用。实现方式为 IR 层把对应指令替换为 profile 函数，并替换或包装 RuSTy 标准库。**每个条目都附有与真实 CODESYS 的差分测试。** |
| ③ PLC 故障 oracle | `SemanticViolationObjective`、PropertyViolation 入口 | **O1** 运行时异常导致停机（仅在 profile 下才会 trap 的情形：启用 CheckBounds 时的越界、访问违例、部分厂商的除零）；**O2** 看门狗或周期超时（按 STG 循环与任务周期设定每周期的迭代上界，可捕获 INC/INC2 这类“逻辑退化导致死循环”）；**O3** 静默数据错误（溢出、截断、除零返回值流向输出、索引或控制条件，用 STG 数据依赖过滤）；**O4** 用户属性（联锁、范围断言） |
| ④ 语义引导搜索 | STG 任务、定向变异、多周期序列 | 搜索目标改为 oracle 位点；增加“攻击者可控输入可达”的过滤 |
| ⑤ 真实运行时自动交叉验证 | 无 | 对每个去重后的报告，自动生成 CODESYS 工程：通过 PLCopenXML 导入 POU，并生成一个按周期回放输入的 PROGRAM；用 CODESYS Scripting（Python，`--runscript`）编译、下载到 CODESYS Control Win V3 或 Linux SL，读取设备日志、异常与输出，给出“确认 / 未复现 / 行为不同”三态标签。这正是 ICSQuartz 自己列出的未来工作。可加入第二个运行时（OpenPLC，或 K-ST 可执行语义）做多方差分。注意 CODESYS 演示模式有运行时长限制，流水线需要处理重启。 |
| ⑥ 调用上下文（加分项） | STG 的调用摘要 | 库函数级 fuzzing 必然产生契约违反类报告。两种降噪方式：从真实调用点提取前置条件；或者在应用级（PROGRAM 的 I/O 映像，跨周期）做 fuzzing |

### 5.5 评估设计

**基准：**
- OSCAT Basic 全量（ICSQuartz 用了 63 个程序）、OSCAT Building、OSCAT Network。
- ICSQuartz 扫描周期集与 ICSFuzz 注入集（有真值）。
- 5–10 个开源的中大型 ST 工程或库，用于回答可达性与实际影响。
- 保真度微基准：为每个 profile 条目写一个最小程序，用于 RQ1/RQ2 的机理归因，同时验证 profile 本身的正确性。

**Baseline：**
- ICSQuartz：官方未修改版本，在 FB 目标上开启扫描周期模式，使用其最佳配置。
- AFL++：修复“种子即崩溃”问题后的配置。
- LibAFL 普通 havoc（使用同一 harness）。
- SemantiST 的各消融版本。
- ICSFuzz/FieldFuzz 只做定性对比。

**实验协议：**
- 所有工具在同一 campaign 中交错调度、单核绑定。
- 每个配置至少 10 次，建议 20 次；真实库每次 24 小时，注入集每次 1 小时。
- **由统一的参考构建做统一重放判定**，不直接采信各工具自带的 oracle。
- 种子分两组：空或零种子一组，各工具自己的最佳种子一组。
- 参照 Klees et al.（CCS'18）与 Schloegel et al.（S&P'24, *SoK: Prudent Evaluation Practices for Fuzzing*）的建议。

**指标：**
- 经 CODESYS 确认、按根因去重的独立缺陷数。
- 报告精度（precision）。
- 首次发现时间（Kaplan–Meier 生存曲线，log-rank 检验）。
- 显著性与效应量：Mann–Whitney U 检验，Vargha–Delaney Â12。
- 覆盖率：用统一的 llvm-cov 构建重放各工具的语料。
- 分类成本：需要人工处理的报告数。

**希望得到的 headline 结论（写作目标）：**“现有流程在 OSCAT 上报告的崩溃中，X% 在 CODESYS 上不成立；SemantiST 的报告精度为 Y%（baseline 为 Z%）；发现 N 个新缺陷，其中 K 个获得 CVE。”

### 5.6 安全故事与威胁模型

- **攻击者能力：** 能写入网络可达的变量（Modbus 保持寄存器、OPC UA 可写节点、HMI 配方或设定值），不能修改程序本身。
- **安全后果：** PLC 停机（可用性，物理过程中断）和静默的错误控制输出（完整性）。这比“内存破坏”更贴近 OT 安全的叙事，也正是 O1–O3 要捕获的。
- **必须回答“库函数的参数是否攻击者可控”：** 在真实工程中统计从 I/O 或网络映射变量到 hazard 位点的数据流可达性（STG 数据依赖加工程级调用图），并给出 2–3 个端到端案例。
- **负责任披露：** 联系 OSCAT 维护方与 CODESYS/CERT@VDE。
- **优先核实：** 你笔记中的 WEEKDAY_TO_STRING、FSTRING_TO_MONTH 与 CVE-2024-6876（MONTH_TO_STRING）同型。先确认 OSCAT 当前版本是否已包含该 CVE 的补丁、补丁覆盖了哪些函数。若补丁后仍然存在，就是“补丁不完整 / 变体”类发现，审稿人认可度很高。

### 5.7 与相关工作的定位

| 工作 | 测试对象 | 执行环境 | Oracle | 真实 PLC 验证 |
| --- | --- | --- | --- | --- |
| ICSFuzz（USENIX Security'21） | CODESYS 控制应用二进制 | 真实或虚拟运行时 | 崩溃（段错误） | 本身运行在真实运行时上，但慢且难以复现 |
| ICSQuartz（NDSS'25） | ST 源码，经 RuSTy + LLVM 重编译 | 原生执行 | ASan / 崩溃 | 一个 CVE 手工验证；自动化列为未来工作 |
| LogicFuzz（NDSS'26） | PLC 固件中的厂商逻辑指令 | 真实硬件 | 日志、LED、通信状态 | 有 |
| Ta'veren（NDSS'26） | PLC 二进制（静态分析） | — | 对外设输入缺少检查（blind-trust） | — |
| K-ST | ST 的可执行形式语义 | K 框架 | 与 CODESYS 的行为一致性 | 可作为参考语义 |
| **SemantiST（建议）** | ST 源码 | 原生执行 + 厂商保真 profile | PLC 故障 oracle（O1–O4） | **自动交叉验证** |

NDSS 近两年连续接收 PLC 相关论文，说明该方向在顶会有受众。上表中除 ICSQuartz 外，其余工作的细节我只看了摘要和项目页，引用前需要通读原文核实。

### 5.8 预判审稿质疑与回应

- **“这只是修 bug 级别的工程，ICSQuartz 已经承认了 RuSTy 的问题。”**
  ICSQuartz 只在附录中定性提及，仍把这些崩溃计入了结果；我们给出首个系统性测量（成因分类、比例、对工具排名的影响），提供自动修正与验证机制，并揭示了前作完全没有涉及的反方向，即漏报。
- **“为什么不直接在 CODESYS 上 fuzz？”**
  吞吐和可观测性都不允许；ICSFuzz/FieldFuzz 的慢与不可复现已有记录。我们采用“快速的近似执行 + 慢速的权威确认”两级结构。
- **“profile 是手写的，能否扩展？”**
  profile 条目数量有限，而且可以用差分 fuzzing 自动发现 RuSTy 与 CODESYS 之间的语义差异。这本身可以作为一个子贡献，顺带向 RuSTy 报告 bug。
- **“只支持一个厂商。”**
  CODESYS 运行时被大量 OEM 采用，覆盖面大；再加一个开源运行时，论证方法的可迁移性。
- **“库函数漏洞不等于系统漏洞。”**
  用 5.6 节的可达性分析和端到端案例回答。

---

## 6. 备选主线评估

| 主线 | 评价 | 建议 |
| --- | --- | --- |
| **B. 深状态、长扫描周期的定向 fuzzing** | ICSQuartz 已有自适应扫描深度与 stale-cycle 变异；现有数据没有显示优势；需要自建长周期基准，容易被认为是“自己造靶子” | 风险高。作为主线 A 中 RQ4 的一个组件结果即可 |
| **C. 应用级跨周期 fuzzing 加攻击者输入建模** | 新颖性尚可，但需要真实 PLC 工程，数据难获取 | 作为主线 A 的 ⑥ 号子贡献，或作为后续工作 |
| **D. 纯算法改进（更好的距离度量、求解器）** | 安全顶会对“又一个定向 fuzzer”的接受度低；与 ICSQuartz 的吞吐差距使对比难以成立 | 不推荐 |

---

## 7. CCF-B 保底方案

**目标会议（已对照 CCF 官网“网络与信息安全”目录）：** ACSAC、ESORICS、RAID、DSN、SRDS、CSF 为 B 类。注意 AsiaCCS、EuroS&P、DIMVA 在该目录中为 C 类。软件工程方向可考虑 ISSRE（B 类）。截稿时间请以当年 CFP 为准。

**最小可行论文（故事不变，范围缩小）：**

1. 完成全部 P0 修复，以及统一的重放与分类流水线。
2. 实现一个 CODESYS profile，至少覆盖字符串、除零/取模、时间、指针算术四类。
3. 实现 O1、O2 两类 oracle。
4. CODESYS 验证可以是半自动的（允许人工操作），但必须给出完整的记录。
5. 评估：OSCAT Basic 全量加 ICSQuartz/ICSFuzz 注入集；每配置 10 次（1 小时或 24 小时）；报告精度、首次发现时间与新缺陷。
6. 至少 3–5 个在 CODESYS 上复现的 OSCAT 缺陷。可以从你笔记里的 INC、INC2、WEEKDAY_TO_STRING、REAL_TO_STRF、`_STRING_TO_BUFFER` 出发，要求工具能重新发现并自动验证它们。

CCF-B 的审稿人主要看三点：问题是否清楚、评估是否可靠、是否有真实缺陷。保底方案只要把这三点做扎实就有机会。

---

## 8. 路线图（建议 16–20 周，含决策关口）

| 阶段 | 周 | 内容 | 关口（go/no-go） |
| --- | --- | --- | --- |
| 0 | 1–2 | P0 修复；统一重放与分类流水线；重跑 smoke，再做全量 1 小时 × 3 次的快速检查 | 三个工具的伪影分类已自动化；SemantiST 的 FB 目标不再在第一次执行时崩溃 |
| 1 | 3–5 | **RQ1 测量**：在 CODESYS Control Win V3 上验证 OSCAT 全部去重后的崩溃，得到真值表；同时写出图 1 与引言初稿 | 伪影率是否显著（预计超过 50%）。显著则走顶会路线，否则转 CCF-B |
| 2 | 5–10 | CODESYS profile；O1–O3 oracle；in-process 执行提速 | 差分测试全部通过；吞吐达到 10⁴ exec/s 以上 |
| 3 | 8–12 | 基于 CODESYS Scripting 的自动交叉验证 | 能端到端自动给出三态标签 |
| 4 | 10–16 | 扩展基准（OSCAT Building/Network、真实工程）；正式 campaign；消融 | 至少 N 个新缺陷经 CODESYS 确认 |
| 5 | 14–20 | 漏洞披露；论文写作；artifact 准备 | — |

**写作顺序建议：** 第 3 周就写出图 1 和引言，用“先写故事”倒逼后续工作，避免再次出现“机制做了很多、故事讲不出来”。

---

## 9. 论文结构建议

1. **Introduction**：图 1（两个方向的鸿沟）；四项贡献——测量研究、保真执行与 oracle、自动交叉验证、真实漏洞。
2. **Background**：ST 与扫描周期；CODESYS 运行时与隐式检查；“重编译后 fuzz”的范式。
3. **Measurement Study**（RQ1、RQ2）：这一节既是 motivation，也是第一项贡献。
4. **Design**：保真执行、PLC 故障 oracle、语义引导、交叉验证。
5. **Implementation**
6. **Evaluation**（RQ3–RQ5）
7. **Case Studies**：2–3 个新缺陷的端到端分析。
8. **Discussion**：厂商差异、局限性、效度威胁。
9. **Related Work**

---

## 附录 A　OSCAT 逐目标分类

“✔”表示该工具至少在 1 个 trial 中报告了崩溃，括号内为重放或日志中的主导类型；“—”表示没有报告；“未启动”指 AFL++ 因初始种子即崩溃而拒绝启动。

| 目标 | SemantiST | AFL++ | ICSQuartz | 根因 | 标签 |
| --- | --- | --- | --- | --- | --- |
| \_BUFFER\_INSERT | ✔（堆溢出，PT/SIZE） | ✔（UTF-8 panic） | ✔（UTF-8 panic，少量全局溢出） | 指针与长度独立变异 | C / T |
| \_STRING\_TO\_BUFFER | ✔（堆溢出 / SEGV） | ✔（UTF-8） | ✔（UTF-8，少量全局溢出） | 同上；SIZE=0 的差一错误需另行证明 | C（可能 G） |
| BUFFER\_COMP | ✔（堆溢出） | — | — | 指针与长度契约 | C |
| CHARNAME | ✔（CONCAT panic） | ✔（UTF-8） | ✔（UTF-8） | RuSTy 标准库 | T |
| CLEAN、DEL\_CHARS、FIND\_CHAR、FIND\_CTRL、FINDP、IS\_CC、IS\_NCC、MIRROR、REAL\_TO\_STRF、REPLACE\_ALL、REPLACE\_CHARS、TRIM、UPPERCASE（13 个） | — | ✔（UTF-8） | ✔（UTF-8） | RuSTy 标准库 | T |
| FINDB\_NUM、FINDB\_NONUM | ✔（`str` 栈溢出） | ✔（UTF-8） | ✔（UTF-8） | ADR→REF 指针缩放 / RuSTy 标准库 | T |
| FSTRING\_TO\_BYTE、FSTRING\_TO\_DWORD | ✔（LEFT\_EXT panic，字面量越界读） | 未启动 | ✔（LEFT\_EXT panic） | RuSTy 标准库 / 代码生成 | T |
| TRIM1、TRIME | ✔（字面量越界读，LEFT\_EXT panic） | 未启动 | ✔（LEFT\_EXT panic） | 同上 | T |
| DT\_TO\_STRF | ✔（DATE panic） | ✔（`__DT_TO_STRF192` 栈溢出，DATE panic） | ✔（同 AFL++） | RuSTy 标准库；编译器临时变量待定 | T / U |
| FILTER\_I | ✔（FPE，多周期） | — | — | TIME 位宽与取值域不一致 | T（待确认） |
| PARSET | — | ✔（全局越界） | ✔（全局越界） | 非规范 BOOL（`0x0a`） | I |
| CTRL\_PI、CTRL\_PID、DRIVER\_1、FLOW\_CONTROL、FLOW\_METER、INTERLOCK、PWM\_DC、RMP\_B（8 个） | ✔（`inst`、`save`、`f6_storage` 栈溢出，第 1 次执行） | — | — | SemantiST FB 布局错误 | H |
| INC、INC2、SIGNAL | ✔（FPE） | ✔（FPE） | ✔（FPE） | 整数 DIV/MOD 0；CODESYS 上表现不同 | G |
| SCHEDULER\_2 | ✔（FPE） | 未启动 | ✔（FPE） | 同上 | G |
| WEEKDAY\_TO\_STRING | ✔（SEGV） | ✔（全局越界 / SEGV） | ✔（全局越界） | 负索引越界读，CVE-2024-6876 同型 | G |

合计 39 个目标至少被一个工具报告过崩溃，其中只有 5 个属于 G。

## 附录 B　复现本次分类

1. **AFL++：** 对 `runs/aflplusplus_full/<suite>/<target>/trial_0X/tool-artifacts/afl/default/crashes/id:*` 中的输入，执行
   `preprocessing/aflplusplus_full/<suite>/<target>/fuzz_target <input>`（设置 `ASAN_OPTIONS=detect_leaks=0`），根据输出中的 `panicked at …` 或 `ERROR: AddressSanitizer: …` 分类。
2. **ICSQuartz：** 对 `runs/icsquartz_full/<suite>/<target>/trial_0X/tool-artifacts/crashes/` 中的非隐藏文件，用同一目标的 AFL++ 二进制按相同方式重放（两者共用 benchmark `harness.c` 的字节布局；扫描周期目标除外）。
3. **SemantiST：** 解析 `findings/sanitizer-logs/*`，对其中 `fuzz_target+0x…` 形式的偏移减 1 后，用 `addr2line -f -e tool-artifacts/fuzz_target` 符号化，并记录 ASan 报告中 `[a, b) 'var' <== Memory access` 的变量名。
4. **根因确认：** 查看 `preprocessing/semantist_full/oscat_basic/<target>/target.ll`，例如在 FINDB\_NUM 中搜索 `getelementptr [81 x i8]`；FB 实例大小按 IR 中的 `%<FB> = type {…}` 与 DataLayout 计算。

## 附录 C　关键证据位置

| 证据 | 位置 |
| --- | --- |
| 未知类型一律当作指针 | `compiler/parser/st.py:613-617` |
| `VAR_BLOCK_STARTS` 缺少 `VAR_GLOBAL` | `compiler/parser/st.py:46-52` |
| FB 字段 C 类型生成 | `compiler/harness/generator.py:115-135` |
| `ADR()`→`REF()` 正则规则 | `compiler/parser/st-to-rusty-converter/scripts/dialect_rules.py:43-47` |
| 启动期全为 objective 时退出 | `fuzzer/engine/src/main.rs:262-277` |
| ForkserverExecutor 执行路径 | `fuzzer/engine/src/main.rs:177-189` |
| exact 违规才成为 objective | `fuzzer/semantic/observers.rs:72-78` |
| 正确的 IR 推导 harness | `benchmarks/generate_harness.py`；各目标的 `harness.c` 中的 `SEMANTIST_INSTANCE_SIZE` |
| ICSQuartz 对 RuSTy 标准库问题的自述与“未来工作” | NDSS'25 论文附录 B 与第 VII 节 |
