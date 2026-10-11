# SemantiST 语义引导算法与实现边界

本文按 2026-10-09 工作区源码核对。实现依据为 Python 静态 planner、Rust 在线调度/反馈/变异器和 C harness；实验配置以当前入口解析结果为准。通过哪些验证以带源码与样本范围的 [2026-10-08 算法验收记录](algorithm_validation_20261008_zh.md) 为准，该历史记录和本文都不是正式实验性能结论或无缺陷证明。

## 1. 定位与数据流

SemantiST 是针对重新编译的 IEC 61131-3 ST 程序的语义引导灰盒 fuzzer。测试输入为结构化字段或扫描周期序列。FUNCTION_BLOCK 的每个输入从新建 FB 实例开始，状态只在同一输入的扫描序列内继承；FUNCTION 输入只调用一次函数。

```text
编译器 AST / 类型信息 → STG → 有界候选路径与任务
                                  ↓
真实输入执行 → AFL 边图 + 语义目标图 + 有界周期/状态遥测
                                  ↓
全局发现、当前输入画像、当前任务进展
                                  ↓
混合任务/队列调度 → 轻量定向变异 / 结构化随机变异
```

算法不包含完整符号执行、全路径约束求解或物理过程仿真。排序分数不是漏洞概率，预算失败不是不可满足证明。

## 2. 静态计划

计划 schema 保持 `semantist.semantic-task-plan/1.0.0`。每个任务保存终点、候选路径、控制义务、数据依赖、状态 transfer、时间要求、可控输入和建模状态。

路径搜索保存最多 8 条普通候选或 4 条 exploration 候选。除路径长度/数量限制外，每次搜索默认最多展开 4096 条边；拒绝的循环边也计入预算。任务的 `path_search` 记录实际展开数、队列峰值、截断原因。未找到路径可能是搜索截断，不能据此声称目标不可达。

每条候选保存按程序顺序排列的 `edge_ids`。在线构造赋值定义时只消费选定路径中引导条件之前的 transfer；相同 waypoint 序列对应多条不同路径时停用有歧义的定义。符号优先使用完整限定名，重复赋值、未知写入或无法确定的 reaching definition 保守回退。

## 3. 三类证据的边界

| 证据 | 用途 | 不能据此推导 |
| --- | --- | --- |
| 全局静态目标集合 | 目标发现、收益计算、终点去重 | 当前输入已完成某条路径 |
| 单个输入的有序事件与实际扫描次数 | 当前 seed 的路径前缀、下一 guide、时间工作量 | 未观测到的分支或完整状态可达性 |
| 有界抽象状态摘要 | 保留相同边覆盖下的状态变化输入 | 数值距离、指定 transfer 已完成或过程安全 |

每次执行先采集证据，再由 LibAFL 分类 corpus/solution。因此正常退出的语义违规也更新全局覆盖、任务状态和本次执行元数据。下一输入会清空共享内存和当前画像，不继承上个输入的证据。

语义 objective 按稳定目标和诊断类别去重；该去重不是根因级 unique bug 统计。Crash、sanitizer、timeout 等仍由各自运行时逻辑处理。

## 4. 默认周期与状态反馈

除独立的 AFL edge map 和 STG semantic map 外，默认另用 40,976 字节共享内存遥测区，包含：

- 真实扫描次数；
- 最多 4096 个按执行顺序记录的 (runtime ID, 扫描序号) 事件；
- 最多 512 对扫描前后抽象状态；
- 启用及溢出标记。

父进程在每次执行前清零。目标侧不为默认反馈写逐事件文件。文本 trace 保留给调试；正常实验不需要开启它才能得到周期和状态信息。固定 benchmark harness 在构建副本上补入同一反馈钩子，原始 fixture 不变；不能安全识别的 FB wrapper 显式构建失败。

抽象投影检查快照字段列表的前 32 项（output、persistent 和 inout），其中指针及非标量 inout 跳过，标量 inout 读取其值。小整数保留细粒度值，大整数按符号和数量级分桶；浮点保留符号、指数和特殊值类别；普通数组/字符串只检查前 16 字节是否包含非零字节。因此它有意合并部分状态，不是完整内存快照或目标相关分支距离。

本次画像最多保留 256 个状态/转换签名；全局最多记忆 4096 个紧凑签名。容量满后不再仅凭新的状态签名扩充 corpus，但当前输入画像仍更新，其他覆盖反馈仍有效。状态摘要不含周期序号，单纯重复相同状态不会无限制造新颖性。

截断遥测只能证明实际保留的正向前缀，不能证明尾部完整、未发生某事件或任意完整执行路径。缺少有序事件的旧目标只能提供平面覆盖，不能跳过路径义务；需要重新构建目标才能获得新反馈。

## 5. Seed、路径前缀和 GuideTarget

`SeedProfile` 保存输入哈希、静态覆盖、有序 `ordered_target_ids`、`ordered_trace_complete`、按周期覆盖、实际周期序号、当前状态签名、affinity 和 parent。

对候选路径 pi 和当前输入的事件序列 T，令：

```text
prefix(pi, T) = pi 的最长前缀长度，且该前缀按顺序出现在 T 中
guide_index = prefix(pi, T)
Guide = pi[guide_index]，若 index == len(pi) 则没有剩余 guide
Remaining = pi[guide_index..]
```

匹配允许中间存在其他事件，是 waypoint 子序列证据，不是完整控制流轨迹等价性。重复 waypoint 按位置计数，例如路径 [a,b,a,v] 和事件 [a,b] 的下一 guide 是第二次 a。禁止用集合去掉重复次数，禁止拼接不同输入的事件推进。

目前匹配可以跨同一输入的多个扫描，不把候选路径的 temporal edge 与扫描边界逐一绑定。例如先 reset、后累计到阈值的输入，可能匹配包含 reset 和阈值分支的候选序列；这不能证明二者在同一扫描可行，也不能证明候选 transfer 链已满足。此近似用于引导，终点发现仍须真实探针命中。

选择路径依次最小化剩余长度、总长度、稳定 ID 序列。实际抽到探索 seed 后重新计算该 seed 的路径和 guide，不沿用最佳 seed 的位置。

一个 LibAFL 变异阶段内 task、guide 及 guide 位置固定。单次执行的有序前缀越过该位置才记成功，每阶段至多记一次。即使后续事件因容量不足截断，已实际看到的正向前缀仍然有效。下一个调度阶段从真正保存的 seed 画像重新计算 guide。

每个任务/候选路径保存一个单调的最长前缀计数。首次更长的连贯前缀也可触发 corpus admission，即使它的静态目标此前被不同输入分别覆盖。反复重现同一前缀不会不断扩充 corpus；目前只跟踪活动任务路径的这项进展。

## 6. 排序与调度

工作量是五个归一化启发式量之和：

```text
EC = 剩余路径上的控制义务数 / NEC
ED = 数据依赖数 × (1 - affinity) / NED
ES = transfer 数 × (1 - state_progress) / NES
ET = max(0, required_steps - completed_steps) / NET
EH = 未满足危险前置目标与终点数 / NEH
Effort = EC + ED + ES + ET + EH
```

各归一化因子为同一 POU 中对应静态量的最大值，至少为 1。EC 和 EH 使用当前 seed 已证实的有序前缀，而不是全局覆盖集合。affinity 仍是该 seed 的 waypoint 集合重叠比例，仅影响估计成本，不推进 guide。只有存在当前状态签名时 state_progress 才取 affinity，否则为 0；该量不表示某条 transfer 已求解。并行时间要求取各自 minimum_cycles - 1 的最大值，不求和；completed_steps 使用真实扫描次数减一。下式的 Gain 才使用全局已发现目标。

```text
Gain = 未发现的 exact violation 指示量
     + 未覆盖 unlocked targets 数
     + min(1, 控制义务种类数 / 7)
Priority = Gain / (1 + minimum_seed_effort) × exp(-0.25 × failed_attempts)
```

优先级并列按 task ID，seed 并列按内容哈希。每个任务缓存最多 4 个候选 seed，新入库画像增量更新缓存；只在失效事件后重排任务。普通选择仍需筛选可调度任务，探索选择可能遍历 seed 索引，不能把整个 Scheduler::next() 宣称为严格 O(1)。

调度保持 75% 语义选择、25% 普通队列；语义分支中保留 15% 任务探索和 20% seed 探索。非探索选择在前四名按 8/4/2/1 抽样。均匀 seed 探索不再先对整个 corpus 按 effort 排序。上述比例是实现参数，需实验评价。

## 7. 预算和任务状态

默认预算为每个 task + path + guide_index + target 1000 次实际目标执行，可由 `--semantic-task-budget` 调整。同一稳定目标在不同路径或不同出现位置分别计费。它以变异阶段为结算窗口，可能超过阈值一个阶段，不是逐输入硬停止。

预算结算归给上一阶段实际执行的上下文，即使调度器已选择另一个任务。该窗口有新 guide 成功时先清除该键的失败预算。普通队列选择显式暂停语义上下文，更新全局发现但不消耗旧任务预算。

连续 3 次预算失败进入 Deferred。新的语义覆盖、corpus 增长或 128 次调度选择后的冷却到期可重新激活；冷却每 32 次调度检查一次。重新激活时失败数最多保留 2，避免立即永久沉睡，也不把旧失败历史全部抹掉。诊断 attempt history 最多保留 1024 项，触限批量删除旧 512 项；任务累计计数保留。

| 状态 | 含义 |
| --- | --- |
| Ready / Active | 可调度 / 当前已选 |
| Covered | 终点由真实探针命中 |
| Deferred | 暂时降权等待重试 |
| Blocked | 当前静态计划无可执行候选路径 |
| Unmapped | 缺少终点探针映射 |

不产生 Infeasible 状态。Covered 只表示该语义终点已观察到；一次 fuzz 运行没有发现违规不证明程序安全。

## 8. 生成与变异

初始生成保留 FUNCTION 最多 16、FB 最多 24 个候选。序列化先输出共享配置，再逐周期输出该周期全部输入字段，符合 harness 按周期 ID 切换执行的合同。

生成、变异和 engine 启动时传给 harness 的 `SEMANTIST_FB_MAX_CYCLES` 统一：默认 256，最高 10000；环境变量无法解析或为 0 时回到默认，超过上限的可解析正整数截为 10000。实验入口则直接拒绝超出 1–10000 的参数。周期增长保留现有短序列探索、前缀复制和结构操作，不直接为所有 seed 填充 256 周期。这个上限是资源约束，不是对所有目标足够的理论深度；独立执行 harness 时仍需自行传入预算相关环境变量。

语义变异占可用语义上下文下的 70%，其余保持结构化随机探索。轻量局部处理包括：

- 整数比较使用 i128 中间表示和 IEC 类型范围，严格比较用可表示的离散邻值；ULINT/LINT 不经过 f64。
- REAL/LREAL 使用各自类型的相邻可表示数，输出可往返解析的文本。
- AND/OR 只做有界局部尝试，并验证整个局部谓词；矛盾、不支持或未改变输入时恢复并回退。
- 除数表达式沿无歧义局部定义做有界反推，最终重新求值验证；不把复合表达式的 reads[0] 冒充可写变量。被赋值或调用改写过的输入不再视为当前程序值，未知写入保留 null 标记并回退。
- 编译器生成的 `member_access` 单变量包装仅在成员名、完整读取符号和类型一致时透明识别；call、cast、array 及复杂成员表达式不按普通变量处理。
- 状态前缀变异只从选定路径 transfer 的读取集关联可控输入，不直接写内部状态；它不求解完整跨周期 transfer 链，也不保证所有既有前置条件保持。

完整配置下，满足多周期 hint 或存在 transfer 输入的上下文，会以 15% 条件概率尝试复制一个非末周期，使序列增长一周期并保留原末周期输入。若未返回，存在 transfer 字段时再以 50% 条件概率修改一个非末周期的相关输入；末周期 goal 尝试失败后还可再次尝试 transfer 输入变异。前述概率只作用于对应分支，不是实际执行中固定的操作占比。

语义分支会先补足受周期上限约束的 hint，其删除操作也检查最低周期数。通用 cycle havoc 保留复制、插入、删除、reset-like、enable toggle 等操作，可以缩短序列，故“每次变异都保持任务最低周期数”不是当前保证。

最多 65,536 个 u64 输入指纹以 FIFO 淘汰，避免每次尝试都永久保存字符串哈希。该过滤是有界启发式去重，存在哈希碰撞可能，淘汰后相同输入允许再次执行；持久语料身份仍使用 SHA-256。

## 9. 消融与当前实验配置

当前 engine 支持 `full` 和六种消融：

| 配置 | 关闭的机制 |
| --- | --- |
| `no_semantic_init` | 语义初始候选生成 |
| `no_targeted_mutation` | 语义定向变异，仍使用语义调度 |
| `queue_havoc` | 语义初始生成、语义调度和语义定向变异 |
| `no_transition_guidance` | transfer 输入引导的非末周期变异 |
| `no_prefix_growth` | 15% 专用前缀增长分支 |
| `no_multicycle_guidance` | 专用前缀增长和 transfer 输入引导两项 |

所有配置保留语义/状态反馈、objective、结构化输入和通用周期操作。`queue_havoc` 不能称为无语义反馈的纯 AFL++；`no_prefix_growth` 仍可通过通用操作增长周期，`no_multicycle_guidance` 也不是单周期执行。具体调用见 [消融说明](../experiments/ablations/README.md)。

批量实验推荐使用仓库根目录的 `python run_experiment.py`：`main` 默认三工具、当前 manifest 全部 90 目标、300 秒和 5 次重复；`smoke` 默认三个代表目标、20 秒和一次。使用 `--run-id` 选择新目录，`--fuzz-time`、`--fuzz-trials`、`--tools`、`--target-id` 和 `--cpus` 覆盖预设。非 `full` 消融必须选择 SemantiST 单工具；旧脚本保持兼容。操作步骤见 [服务器手册](server_experiment_runbook_zh.md)。

文件级 FB state trace 默认关闭，stale threshold 默认 0，前文的共享内存反馈不受其影响。在线预算包含启动与语义初始生成，原生执行计数以 testcase 为单位，不能与 FB 扫描次数混用。正常运行严格校验通过后自动输出逐 trial `metrics.csv`/`metrics.jsonl`；公共重放、覆盖和统计分析默认留待手动执行。语义目标命中、公共 crash 候选、重放确认和根因级独立漏洞是不同口径，详见 [实验规范](experiment_standard.md)。

## 10. 吞吐平衡与验收

当前实现使用固定容量遥测、有界尝试输入缓存和不预排序全 corpus 的均匀 seed 探索。默认语义汇总日志主要记录全局新增目标或状态，不为每次重复执行写 JSON；显式 debug trace 仍可产生逐事件文件 I/O。每输入遥测解析、状态摘要和画像处理仍有开销，不能从代码变化直接断言总体提速。

验收应包含实际 LibAFL solution 分类、不同 seed/全局覆盖隔离、重复 waypoint、预算切换、有界规划、IEC 边界、真实 harness 两字段扫描顺序、真实 ST 超过 64 周期目标、反馈溢出重置，以及固定 CPU、相同编译器/参数/种子下的前后吞吐、内存和日志增长对照。

必须报告样本范围和未覆盖条件。机械性问题修复与实验竞争力是两件独立的事；局部验收不能代替多目标、多随机种子的正式比较和消融实验。

## 11. 源码核验入口

- 静态计划：`fuzzer/semantic/planning.py`；路径与预算：`fuzzer/semantic/runtime.rs`、`fuzzer/stages/semantic_budget.rs`。
- 逐执行证据与遥测：`fuzzer/engine/src/main.rs`、`fuzzer/semantic/observers.rs`、`compiler/instrumentation/runtime/semantic_coverage.c`；Harness 与固定 fixture 升级：`compiler/harness/generator.py`。
- 调度、生成、变异与消融：`fuzzer/scheduler/engine.rs`、`fuzzer/corpus/generation.rs`、`fuzzer/engine/src/mutations.rs`、`fuzzer/engine/src/config.rs`。
- 实验预设与导出：`experiments/comparison/run_full_comparison.py`；计量：`experiments/comparison/lib/metrics.py`；SemantiST 适配：`experiments/comparison/adapters/semantist.py`。
