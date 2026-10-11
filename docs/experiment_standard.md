# SemantiST 对比实验标准

核对日期：2026-10-09。本文规定当前对照实验的运行和测量合同；源码、静态支持清单或本地测试不能替代服务器上的正式实验。已有验证证据另见 [验收记录](algorithm_validation_20261008_zh.md)，不作为本文预设的实验结果。

## 1. 运行方式与统一条件

每条试验独立启动、独立保存结果，全部结束后离线比较。首选入口是根目录 `run_experiment.py`，以 `--experiment smoke|main` 选择配置；默认主实验为三工具、90 目标、300 秒、5 次。入口先准备所有目标，再按 `--cpus` 池调度在线试验；只有显式 `--serial` 才逐条串行。需要分开运行时加 `--tools 工具名`，仍使用同一入口和指标口径。

每次使用新的 `--run-id`，预检也使用单独的新 ID。所有入口均拒绝已有实验目录，已删除 resume、预处理复用和 overwrite 选项。失败后保留原产物，人工处理原因，再用新 ID 重跑选定的一组试验。默认采集原始记录，严格验证成功后导出逐 trial 指标；离线比较、replay（输入重放）/coverage（覆盖率）另行执行。

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

合同版本：`semantist.independent-tool-metrics/1`。权威文件是逐次试验的 `run-result.json`；通用字段位于 `paper_metrics`。`metrics.jsonl` 和 `metrics.csv` 是导出文件。支持目标的无效试验会使在线入口失败；静态不支持的条目保留原因，不能据“入口成功”推断所有目标均受支持。每个所选工具至少须有有效试验。

| 字段 | 定义 |
| --- | --- |
| `valid_run` | 支持该目标、完整完成正预算、无失败原因，且有正的原生执行计数；提前退出不算有效 |
| `crash_found` | 预算内观察到原生崩溃候选；不包含语义目标命中、超时、OOM 或仅有运行器非零退出 |
| `first_crash_executions` | 与首次崩溃候选关联的原生执行计数；无可靠计数时为 `null` |
| `first_crash_time_seconds` | 从启动到采集器首次观察到该候选的单调时钟时间 |
| `first_crash_input` | 对应的实际输入文件；缺失为 `null`，不以日志或报告 JSON 顶替 |
| `total_executions_reported` | 按 `total_executions_window` 记录的最后原生累计计数，可能落后于停止时刻 |
| `execs_per_sec` | 上一项除以在线实际耗时，属于基于原生报告的吞吐量 |
| `replay_confirmed_crash_found` | 独立重放是否确认；尚未验证或无法重放时保留未知状态 |

执行计数单位是 native executor testcase（工具执行器的一次测试输入），不是 PLC 扫描周期。保留 `execution_model`：`single_cycle`（单周期）、`stateful_semantic_sequence`（有状态输入序列）、`scan_cycle`（持续扫描周期）。完整工具效率可以比较，但一次输入包含的工作量可能不同，不能表述为完全相同的函数调用次数。

首次发现计数来源：

- SemantiST：引擎在 finding report（发现报告）中写入 `executions`。采集器也支持同一次运行的 runtime finding event（运行时发现事件）；不假定历史日志存在。
- AFL++：读取该崩溃文件名中的 `execs`。
- ICSQuartz：读取对应 `.metadata`（输入元数据）中的 LibAFL testcase 执行计数。缺失或未写完整时记为缺失，元数据文件本身不算崩溃输入。
- 可选 ICSFuzz：现有包装日志无法可靠关联首次崩溃计数和完整状态输入时，保留缺失；不能按平均速率估算。

所有候选计数可用时，用原生累计执行计数确定发现顺序；否则用首次观察顺序，并在 `first_crash_order_source` 标明。计数、时间和输入始终来自同一候选。同一次轮询看到多个文件时，计数可消除文件名字典序造成的错序；无法消除的顺序不确定性不伪装为精确值。

首次观察时间包括文件写入、轮询、容器检查和调度延迟，是发现时间的观测上界，不承诺毫秒级精确触发时间。首次观察超过预算的候选保留为 `late_crash_artifact_count`，不计入预算内成功。没有观察时间的候选单列为未测量。晚到的元数据可补全已观察候选的计数，但不改写首次观察时间。

ICSQuartz 总执行数使用预算内最后观测到的原生统计，停止期间的额外执行只保留为诊断。其他工具使用停止后的原生快照，可能含清理尾段。`total_executions_window` 明确区分这两种口径；计数都可能落后于实际执行，吞吐量不能称为预算边界上的精确值。

`measurement-events.jsonl` 由本次运行在观察到新发现或元数据变化时创建并追加；没有候选事件时可能不存在。它提供实时采集证据，不用于补造历史观察时间。

## 3. 离线比较与解释

`analysis/compare_tools.py` 接受一个多工具目录，或多个独立工具目录；输入合计至少两个工具，且 SemantiST 必须为 `configuration_id=full`。它读取权威逐次运行文件，重新生成指标，拒绝缺失试验、重复工具/目标/试验编号、不同指标合同，以及共同支持目标上的源码包、时间预算、超时、重复次数或配对种子不一致。消融指标也由运行器逐目录自动导出，手动分析时按 profile 区分，不能混入主比较。

按目标、工具和执行模型分别输出成功率、首次崩溃计数/时间中位数、缺失计数数量、失败数量及正常提前结束数量。成功率分母为有效试验，失败数量必须一起报告。中位数仅针对成功且该项测量存在的试验，列名带 `successes_only`；未发现不能填成零秒或零次。

直接多工具性能结论只使用 `common_supported_target=true` 的目标。共同支持交集为空时保持为空。其他目标用于报告支持性和失败原因。执行模型、超时模型及 CPU 槽位差异列在 `comparability_notes`，论文须披露。

原生候选、重放确认和独立漏洞是三个概念。崩溃输入数量不能直接称为 unique bugs（独立漏洞数量）。没有根因去重时不宣称独立漏洞数。离线 replay 可另行执行；无法重建持续状态历史、缺少独立目标或输入时记为未验证，不能抹去原生发现。启动/解析失败、普通非零退出及终止信号不会自动确认崩溃。

覆盖率分析不放入默认流程。各工具 native edge slots（自身插桩边编号）不能直接当作共享覆盖率指标。

## 4. Linux 上的执行顺序

命令见 [实验入口说明](../experiments/comparison/README.md) 和 [服务器操作手册](server_experiment_runbook_zh.md)。先检查测试文件并构建镜像，然后运行 `--experiment smoke`：三工具、三个代表目标、各一次短预算。烟测没有发现崩溃是允许的；采集正确性另由人工构造的回归测试检查，不假定任意目标必定触发漏洞。首次服务器部署建议再对全 90 目标进行一次短预算验收。

烟测通过后，用新的 run id 启动完整实验，统一预算和重复次数。默认 300 秒、5 次只是起始配置；增加重复次数时各工具同步增加。全部结束后离线比较。本标准不把静态支持清单或本地单元测试标为已完成的 Linux 实验。

Ctrl+C 可能等待当前批试验完成预算后收尾；预处理构建被中断时，人工检查残留容器再重跑。部署、Git 操作、分析和失败重跑均可按服务器手册手动完成，无需额外自动化。
