# SemantiST 对比实验标准

更新日期：2026-10-12。本文规定当前对照实验的运行和测量合同。本次最终 `post_run` 版本仅修改和审阅源码与文档，未运行测试、烟测、编译或验证实验，尚未运行验收。已有历史验证证据另见 [验收记录](algorithm_validation_20261008_zh.md)，不覆盖这一版本。

## 1. 运行方式与统一条件

每条试验独立启动、独立保存结果，全部结束后离线比较。首选入口是根目录 `run_experiment.py`，以 `--experiment smoke|main` 选择配置；默认主实验为三工具、90 目标、300 秒、5 次。入口先准备所有目标，再按 `--cpus` 池调度在线试验；只有显式 `--serial` 才逐条串行。需要分开运行时加 `--tools 工具名`，仍使用同一入口和指标口径。

每次使用新的 `--run-id`，预检也使用单独的新 ID。所有入口均拒绝已有实验目录，不自动重试、resume、复用预处理或 overwrite。失败后保留原产物，人工处理原因，再用新 ID 重跑选定的一组试验。每批结束保存已有原始记录和逐 trial 指标，包括失败状态；完整性检查失败时仍导出已有数据并返回非零，不能把指标表存在视为有效矩阵。离线比较、replay（输入重放）/coverage（覆盖率）另行执行。

采集模式为 `collection_mode=post_run`。在线预算内不循环扫描候选、统计或日志，不做计数停滞 watchdog；停止并确认清理后再读取原生快照。旧 `--observer-poll-interval-ms` 参数仅兼容解析，传入值被忽略，实际记录为 `0`。

`--experiment-id` 与 `--trials` 保留为 `--run-id` 和 `--fuzz-trials` 的别名。预设由 `experiments/comparison/run_full_comparison.py` 的 `EXPERIMENT_PRESETS` 定义；显式目标列表替换预设列表，显式时间和次数覆盖预设默认值。预设名记录在 `environment.json` 与 `experiment-manifest.json` 的 `experiment` 字段，实际选定目标、预算、配置及排程另行保存。

需要统一：

- 同一版本的 benchmark（测试目标）文件包。每条记录保存源码和全部 fixture（测试配套文件）的 SHA-256，离线比较检查一致性。
- 相同的在线时间预算、单次执行超时和重复次数。同一服务器、同类 CPU、相近负载；保留主机、镜像、源码版本和 CPU 分配记录。
- 相同的指标定义。编译和静态准备时间单独记录；在线计时从工具启动前开始，包括容器启动、原生初始化、初始输入生成和校准。停止和清理不延长名义预算。
- 保留各工具输入格式、初始 seed（种子输入）和完整搜索能力。SemantiST 初始生成器的收益属于完整工具收益；分离其贡献应另做消融实验。

权威目标清单为 `benchmarks/external/manifest.json`。每个目录提供：

```text
target.st / program.st / harness.c
semantist_harness.c / semantist.seed
```

harness（把输入传给被测函数的驱动程序）由 benchmark 固定提供。允许工具专用的输入解析和调用包装，但不能改变目标函数语义或加入人工漏洞断言。哈希一致只证明使用同一文件包，不自动证明编译链语义等价。

SemantiST 读取固定结构化种子并在在线预算内进行语义初始生成；AFL++ 使用字节种子；ICSQuartz 使用自身初始输入生成器。`initial_seed_provider` 和 `rng_seed_status` 记录具体策略及随机种子是否实际应用，不假设各工具都有相同生成器或随机数控制接口。

SemantiST 默认周期上限为 256；比较配置时保持 `--semantist-fb-max-cycles` 一致。默认关闭文件状态 trace，共享内存抽象状态反馈仍生效。ICSQuartz 使用仓库内固定目标和基线镜像，不再要求外部 checkout；可选 ICSFuzz 仍需外部 CODESYS 配套工件和校准。

对照实验使用 ASan 作为共同内存错误检测器，不为部分工具单独启用 UBSan。SemantiST 的 exact 语义违规仍可作为自身搜索反馈和发现证据，但不会自动计入共同指标的原生崩溃。不同工具的执行模型和 sanitizer 配置限制见 [实验接口说明](../experiments/comparison/README.md#measurement-settings)。

每次试验运行一个 fuzz worker（实际执行变异测试的进程），并限制在一个逻辑 CPU。ICSQuartz 保留原生 Launcher；运行器把宿主机分配的单核集合映射为容器内可见核序号 `0`，broker/respawner 与该客户端共享该核，不另增加 fuzz worker。一个 runner 中的不同工具共享 CPU 池；多个 runner 必须使用互不重叠的 CPU 池，进程之间没有全局核锁。

## 2. 同一份指标

合同版本：`semantist.independent-tool-metrics/2`。权威文件是逐次试验的 `run-result.json`；通用字段位于 `paper_metrics`。`metrics.jsonl` 和 `metrics.csv` 是导出文件。不要将 `/2` 的停止后快照与 `/1` 的在线轮询数据混合比较。支持目标的无效试验会使在线入口最终返回非零；静态不支持的条目保留原因，不能据“入口成功”推断所有目标均受支持。每个所选工具至少须有有效试验。

| 字段 | 定义 |
| --- | --- |
| `valid_run` | 支持该目标、完整完成正预算、候选采集完整、无失败原因，且有正的原生执行计数；提前退出不算有效 |
| `crash_found` | 停止后快照中存在原生崩溃候选；可能含停止尾部，不能断言恰在预算截止前发现；无候选且采集不完整时为 `null` |
| `first_crash_executions` | 与首次崩溃候选关联的原生执行计数；无可靠计数时为 `null` |
| `first_crash_time_seconds` | 当前模式没有在线观察，统一宿主机首次发现时间为 `null` |
| `first_crash_native_time_seconds` / `first_crash_native_time_origin` | 对应候选的原生时间与起点，存在时单独保留；不替代统一宿主机时间 |
| `first_crash_input` | 对应的实际输入文件；缺失为 `null`，不以日志或报告 JSON 顶替 |
| `total_executions_reported` | 停止后读到的最后原生累计计数，可能落后于实际执行，也可能包含停止尾部 |
| `execs_per_sec` | 上一项除以在线实际耗时，属于基于原生报告的吞吐量 |
| `replay_confirmed_crash_found` | 独立重放是否确认；尚未验证或无法重放时保留未知状态 |

执行计数单位是 native executor testcase（工具执行器的一次测试输入），不是 PLC 扫描周期。保留 `execution_model`：`single_cycle`（单周期）、`stateful_semantic_sequence`（有状态输入序列）、`scan_cycle`（持续扫描周期）。完整工具效率可以比较，但一次输入包含的工作量可能不同，不能表述为完全相同的函数调用次数。

首次发现计数来源：

- SemantiST：引擎在 finding report（发现报告）中写入 `executions`。采集器也支持同一次运行的 runtime finding event（运行时发现事件）；不假定历史日志存在。
- AFL++：读取该崩溃文件名中的 `execs`。
- ICSQuartz：读取对应 `.metadata`（输入元数据）中的 LibAFL testcase 执行计数。缺失或未写完整时记为缺失，元数据文件本身不算崩溃输入。
- 可选 ICSFuzz：现有包装日志无法可靠关联首次崩溃计数和完整状态输入时，保留缺失；不能按平均速率估算。

所有崩溃候选计数可用时，用最小原生累计执行计数确定首次候选，并在 `first_crash_order_source` 标为 `native_execution_counter`。否则首次候选及其计数、输入保留未知，不使用文件名字典序或停止后的读取顺序冒充发现顺序。对应的原生时间、计数和输入来自同一候选。

当前模式不记录在线首次观察时间，也无法把停止后看到的候选准确切分为预算内和预算外。`finding_window` 记录 `native_stop_snapshot_may_include_shutdown_tail`，`late_crash_artifact_count=null`。原生工具的时间可能从 worker、client 或引擎开始计时，起点不同，不能直接拼成统一跨工具首次发现时间。

所有工具的总执行数均来自停止后的原生快照。`total_executions_window` 使用 `native_stop_snapshot_may_include_shutdown_tail`；吞吐量是该快照计数除以在线耗时，不能称为预算边界上的精确值。原生计数、候选、元数据继续保存；缺失或无法解析的字段保持未知，不估算为零。

`post-run-findings.json` 保存停止后的候选快照，不提供实时首发现证据。旧批次中的 `measurement-events.jsonl` 属于旧口径，不用它补造本次运行的观察时间。`candidate_collection_complete=false` 表示未完成候选采集，不能将空列表解释为已确认没有发现。

每批结束保存 partial `raw-runs.jsonl` 和 `campaign-status.json`，后者列出 `completed`、`failed`、`not_started`。已确认清理的单 trial 工具或采集失败继续后续批次；基础设施失败、清理未确认或结果无法持久化时停止。失败记录和未启动条目保留，最终无效矩阵不会静默聚合。

## 3. 离线比较与解释

`analysis/compare_tools.py` 接受一个多工具目录，或多个独立工具目录；输入合计至少两个工具，且 SemantiST 必须为 `configuration_id=full`。它读取权威逐次运行文件，重新生成指标，拒绝缺失试验、重复工具/目标/试验编号、不同指标合同，以及共同支持目标上的源码包、时间预算、超时、重复次数或配对种子不一致。消融指标也由运行器逐目录自动导出，手动分析时按 profile 区分，不能混入主比较。

按目标、工具和执行模型分别输出成功率、首次崩溃计数/时间中位数、缺失计数数量、失败数量及正常提前结束数量。成功率分母为有效试验，失败数量必须一起报告。中位数仅针对成功且该项测量存在的试验，列名带 `successes_only`；未发现不能填成零秒或零次。

直接多工具性能结论只使用 `common_supported_target=true` 的目标。共同支持交集为空时保持为空。其他目标用于报告支持性和失败原因。执行模型、超时模型及 CPU 槽位差异列在 `comparability_notes`，论文须披露。

原生候选、重放确认和独立漏洞是三个概念。崩溃输入数量不能直接称为 unique bugs（独立漏洞数量）。没有根因去重时不宣称独立漏洞数。离线 replay 可另行执行；无法重建持续状态历史、缺少独立目标或输入时记为未验证，不能抹去原生发现。启动/解析失败、普通非零退出及终止信号不会自动确认崩溃。

覆盖率分析不放入默认流程。各工具 native edge slots（自身插桩边编号）不能直接当作共享覆盖率指标。

## 4. Linux 上的执行顺序

命令见 [实验入口说明](../experiments/comparison/README.md) 和 [服务器操作手册](server_experiment_runbook_zh.md)。本次仅更新实验脚本，已有工具链镜像可以复用；Windows 提交推送后，在干净的服务器工作树更新源码，以全新 `main_postrun_...` ID 启动。无需为本次修改强制追加短测，也不把此前的测试记录当作本次验收。

完整实验统一预算和重复次数。默认 300 秒、5 次只是起始配置；增加重复次数时各工具同步增加。`smoke` 预设仍可自行选择，非正式实验的前置条件。全部结束后离线比较。本标准不把源码检查标为已完成的 Linux 实验。

Ctrl+C 可能等待当前批试验完成预算后收尾；预处理构建被中断时，人工检查残留容器再重跑。部署、Git 操作、分析和失败重跑均可按服务器手册手动完成，无需额外自动化。
