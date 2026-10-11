# 算法修订与验收记录（2026-10-08）

**2026-10-08 算法修订的验收结论：在下述覆盖范围内通过，可作为服务器预验收的候选。** 当时完成了 `semantist` Rust 引擎包的 72 项测试、117 项 Python 测试和 20 项子测试，以及真实 ST 分支命中、长周期遥测、固定 harness 烟测和三种负载的配对吞吐对照。它不是整个 Rust 工作区、全部目标或任意长期运行的通过证明，也不替代服务器干净构建及正式实验。

**2026-10-09 文档复核补充：** 本地仍可读取以下日志及 JSON 证据；`tested-source-manifest.json` 中 37 个算法/harness/runtime 文件的换行归一化 SHA-256 与当前工作树一致。之后实验接口增加了预设入口，最近保存的 Linux `tests/unit + compiler/harness` 回归为 **176 passed、112 subtests passed**，日志为 `artifacts/algorithm_validation_20261008/preset-final-tests.log`。该数字是后续接口回归的范围，不能覆盖或替换下文算法机制、性能实验各自的版本和条件。本次文档核对没有重跑这些长期或编译实验。

## 1. 范围与基线

算法目标是修正执行证据、路径进展、目标归因、IEC 边界和扫描序列合同，同时控制额外工作量。没有引入 SMT、完整符号执行或全状态图搜索。

基线是算法修订前的宿主机工作树副本，不是单独的 Git HEAD，也不是现成旧镜像中的 engine：`artifacts/algorithm_baseline_20261008/`。当时基准 HEAD 为 `ee4ed261d66636ff161efd13c4e0fbfc2e757eee`，原有实验接口/消融修改已保留。该 SHA 不是包含后续全部修改的发布版本；冻结时须记录实际提交。

当前实现说明见 [语义引导算法](stg_semantic_ranking_zh.md)，修复前的问题与反例见 [历史审查记录](algorithm_review_20261008_zh.md)。

## 2. 已落地的修订

| 问题 | 当前实现 | 主要位置 |
| --- | --- | --- |
| 初始种子字段交错导致多执行扫描 | 共享字段在前，逐周期输出完整字段 | `fuzzer/corpus/generation.rs` |
| 全局集合或其他 seed 进度污染当前 guide | 单输入有序前缀；实际选中 seed 后重算 | `fuzzer/semantic/runtime.rs` |
| 重复 waypoint 丢失次数 | 按 prefix/index 推进，保留重复位置 | `fuzzer/semantic/runtime.rs` |
| solution 跳过普通语义 feedback | objective 分类前采集证据，随后复用 novelty | `fuzzer/semantic/observers.rs` |
| 成功与预算归错任务 | 阶段内固定 guide，结算真实上一阶段，成功先清预算 | `fuzzer/stages/semantic_budget.rs` |
| 默认缺周期/状态反馈 | 固定容量 SHM、真实扫描序号、抽象状态；补齐固定 harness 构建路径 | `compiler/harness/generator.py`、`compiler/scripts/build_target.sh` |
| 整数/浮点严格边界失效 | IEC 范围 + 精确整数/可表示浮点邻值；验证局部结果 | `fuzzer/engine/src/mutations.rs` |
| 复杂表达式与混合路径定义不可信 | 明确变量节点、限定符号、选定路径顺序；歧义回退 | mutations / observers / runtime |
| 真实编译器变量包装使定向变异失效 | 验证 member、完整 reads 和类型后识别单变量 member_access；拒绝其他包装 | `fuzzer/engine/src/mutations.rs` |
| 硬编码 64 周期 | 生成/变异/engine 统一上限，默认 256、最高 10000 | generation / mutations / main |
| 搜索、去重、日志成本 | 4096 边展开预算、有界输入/状态缓存、仅新颖事件写日志 | planning / observers |
| 任务无新边后长期延期 | corpus、coverage、128 次调度冷却可触发重试 | runtime |

遥测溢出保留正向前缀证据，不把缺失尾部当完整轨迹；新 prefix 只按任务/候选保存一个单调最大值。状态摘要有意合并部分数值和字段，不表示完整状态等价或目标距离。

## 3. 已执行的证据

| 检查 | 实际结果 | 能证明的范围 |
| --- | --- | --- |
| 修复前工作树的 `semantist` 包 release build / Rust tests | 通过，36 个测试 | 基线可构建、可对照；不是修复版证明 |
| 算法修订后 `semantist` 包 release build / Rust tests | 通过，72 个测试 | solution 分类、typed mutation、种子隔离、重复 waypoint、预算切换及真实生成 C wrapper 等；未包含 `compiler/stg` 包 |
| 算法验收当时的 tests/unit + harness Python 测试 | 117 passed、20 subtests passed | planner、harness 和当时实验接口/消融回归；包含生成 C wrapper 编译运行 |
| before/after 真实 ST 构建 | 两版函数和函数块成功构建 | 编译链可用；执行正确性另由下面的运行检查证明 |
| 真实 ST 分支引导 | 初始 X=0/Y=0，150 次调度，4/4 分支结果命中，保存 X=42 witness | 真实编译器 plan 的 member_access 兼容和定向引导可用 |
| 真实 ST 长周期遥测 | 第 99 次扫描未命中，第 100 次命中；256 上限、1000 溢出、后续输入清零均通过 | 私有计数器和真实扫描反馈正确；不是声称 fuzzer 在短预算内自主发现第 100 周期 |
| 全部固定 harness 兼容性 | 90 个检查通过，其中 42 个 FB 补钩子；幂等且原 fixture 不变 | ABI/模板匹配与注入；未逐个 C 编译运行 |
| 固定 OSCAT TOGGLE | 真实构建与 engine 烟测通过，9 个新增目标事件、16 个抽象状态事件 | 正式固定 harness 路径默认收到周期/状态反馈，不依赖文本 runtime trace |
| 测试源码一致性 | 37 个算法/harness/runtime 源码文件与已测试副本逐一相同 | 排除测试旧副本、报告新源码；只去掉两版相同的最终执行计数输出 |
| 该次验收的差异检查 | `git diff --check` 通过 | 文本差异格式，无算法正确性含义 |

本地证据目录为 `artifacts/algorithm_validation_20261008/`。`build-before.log` 和 `build-after.log` 分别记录 36/72 个 Rust 测试通过；`python-tests.log` 记录 117 个测试及 20 个子测试通过。`fixed-harness-check.json`、`telemetry-e2e.json`、`guidance-e2e/result.json` 和 `tested-source-manifest.json` 分别保存兼容性、长周期、真实引导和源码一致性证据。性能测试用 after 引擎 SHA-256 为 `12dc466231004a26f763d7f8036ce62d5dcedb727151479040c71d61cfa7e725`。

## 4. 性能对照与中间问题

第一次配对测量虽未出现吞吐下降，却暴露函数样例覆盖从 4/4 降至 3/4：单元测试使用裸 variable，而真实编译器生成 member_access。该中间结果没有作为最终验收；已修复兼容性、补充 3 个 Rust 回归，并新增 `tests/e2e/algorithm_guidance_check.py` 证明真实 X=42 分支恢复。

最终对照采用相同 CPU、ST 源码、编译器、sanitizer、初始种子、RNG seed 和迭代预算，交替前后顺序，重复 5 次。吞吐用实际目标执行次数除以整次运行耗时，包含启动、调度、过滤和记录成本；不同算法的语料、执行次数、扫描长度可以不同，不能把它解释成固定输入的执行器微基准。

最终结果如下。CPU 固定为容器可用集合中的 CPU 0，使用相同的 ASan 配置和本地 Linux `/tmp` 运行目录。表中均为 5 次重复的中位数，RSS 仅采样 fuzzer 父进程，不含目标进程，不是整容器峰值。

| 负载与调度迭代预算 | before exec/s | after exec/s | after / before | RSS before → after（KiB） | 日志 before → after（字节） |
| --- | ---: | ---: | ---: | ---: | ---: |
| ALGORITHM_BRANCH，150 | 274.61 | 285.73 | 1.0405 | 8448 → 9088 | 1670884 → 795 |
| ALGORITHM_STATEFUL，3000 | 53.80 | 155.81 | 2.8959 | 8704 → 12544 | 166138 → 8439 |
| OSCAT TOGGLE，10000 | 29.68 | 40.54 | 1.3659 | 8320 → 9600 | 126673 → 10495 |

TOGGLE 的 1000 次调度预跑中位数为 139.11 → 170.64 exec/s；因运行较短又延长至 10000 次，最终采用上表。布尔输入会大量重复，迭代预算增长后可能主要增加过滤/调度开销，所以 exec/s 随预算下降并不代表目标二进制单次执行变慢。上述数据衡量完整搜索过程，不能混用不同预算直接比较速度。

性能结论是：这三种本地负载的中位吞吐未下降，新增反馈的成本在样例中由日志、缓存及调度改进抵消；内存有小幅增加。TOGGLE 长预算五组配对速度比分别为 2.218、1.280、0.888、1.541、1.203，其中一组下降约 11.2%。因此不声称任意随机种子都更快，也不将小样本中位数当作统计显著性或全基准提速结论。

覆盖交叉检查：函数样例每组前后均为 4/4 个分支结果；TOGGLE 每组前后均观察到相同 9 个目标；状态样例每组前后均为相同 10 个目标。状态样例的短预算 fuzz 尚未自主发现第 100 周期的深分支，另有一个在本样例守卫下不可到达的加法溢出目标也未命中；100 周期的执行、遥测及越过旧 64 周期上限的能力由独立机制测试证明，不能把它写成深层目标自动发现的效果证明。

合成负载的逐次记录位于 `performance-final/measurements.json`，汇总为 `performance-final/summary.json`；真实 OSCAT 的逐次记录、覆盖及冻结哈希位于 `oscat-toggle-performance/`。较早的 `pilot/`、`performance/` 为中间版本数据，不作为最终修复版验收证据。各次结果保存引擎、目标和初始种子的 SHA-256，以及真实执行次数、耗时、命令和日志。

实际使用的本地验证脚本（不是部署脚本）：

```text
artifacts/algorithm_validation_20261008/build_engine.py
artifacts/algorithm_validation_20261008/prepare_targets.py
artifacts/algorithm_validation_20261008/check_fixed_harnesses.py
artifacts/algorithm_validation_20261008/compare_throughput.py
artifacts/algorithm_validation_20261008/compare_oscat_toggle.py
```

构建/性能脚本使用当前本地验证容器中的 `/audit` 只读源码和 `/validation` 产物目录。`build_engine.py` 在临时副本中添加同样的最终执行次数输出，不改产品算法。性能脚本把在线 I/O 放在容器本地临时文件系统，结束后导出测量；这能避免 Windows 挂载时延支配结果，但仍需在最终服务器复核。

版本化的真实执行检查入口为 `tests/e2e/algorithm_guidance_check.py` 与 `tests/e2e/algorithm_telemetry_check.py`，二者使用 `tests/e2e/fixtures/algorithm_feedback.st` 的构建产物。基线副本及完整测量数据属于本地 `artifacts/`，不随 Git 自动推送；如需转移本次审查证据，应另行保存该目录。

较早的验证曾因审批服务账户用量限制暂时中断，现已恢复，上述实际结果来自恢复后完成的运行。临时 OSCAT 验证脚本也经历了运行库路径和离线 lock 裁剪适配；这些是验证环境问题，不作为算法通过证据。

## 5. 仍需披露的算法边界

工作量是结构启发式，没有连续数值分支距离；状态签名最多观察有限字段，并设置全局容量；路径规划是有界近似；局部变异不保证复杂联合条件和完整跨周期约束可解；普通探索及未激活路径不会获得同样的路径前缀保存策略。有序子序列可跨同一输入的扫描，不证明某静态路径在单扫描可行，也不证明完整 transfer 链成立。这些限制不必都通过增加重型机制解决，应在正式实验中用机制消融、跨周期目标及多次运行量化。

验收通过也只意味着已知重大实现缺陷在所覆盖条件下得到修复，不是完整 PLC 环境安全证明，也不能替代主实验的竞争力证据。

## 6. 冻结与服务器衔接

本轮没有引入 SMT、完整符号执行或无界状态搜索。算法优势仍是编译器语义、IEC 类型变异、单输入多扫描和危险操作真实探针；修订保证这些信息在反馈、调度与变异之间按一致合同传递，并控制热路径成本。

冻结前以 `git status` / `git diff` 检查实际工作区，不把本文基准 HEAD 当作已包含全部修订的提交。服务器须从冻结提交重新构建镜像、Rust 引擎和所有 SemantiST 目标，再进行短测；首次部署建议全目标短验收。新的遥测 ABI 不应混用旧目标或旧 preprocessing。当前操作入口为 `run_experiment.py --experiment main` 或 `--experiment smoke`，完整命令见 [服务器操作手册](server_experiment_runbook_zh.md)。

后续本地接口 QA 另有 `artifacts/i8/s3/qa-summary.json`（三工具、三目标、9 条 20 秒有效记录）和 `artifacts/i8/p3/qa-summary.json`（ICSQuartz 两目标、2 条 180 秒有效记录）。它们使用本地验证镜像和该轮已编译目标，带有文件哈希及镜像身份记录；不是干净服务器冷构建，也不是上述 before/after 性能实验或完整 90 目标主实验。公开入口仍不提供预处理自动复用。
