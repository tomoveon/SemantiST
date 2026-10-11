# SemantiST 文档索引

本目录于 **2026-10-09** 按当前工作树核对。使用说明描述当前行为；带日期的审查、验收和漏洞记录保留当时的对象、版本与证据，不作为最新环境已通过验证的保证。

## 使用与实验

| 需要了解什么 | 文档 |
| --- | --- |
| 本机保存代码、服务器构建、短测、多核运行和结果分析 | [服务器实验操作手册](server_experiment_runbook_zh.md) |
| 在线预算、原生执行数、崩溃候选、有效性和比较口径 | [实验标准](experiment_standard.md) |
| 环境、初始化、镜像与复现要求 | [Reproducibility](reproducibility.md) |
| 通用流水线的漏洞证据、重放、去重和报告 | [Vulnerability reporting](vulnerability_reporting.md) |
| 主实验参数和输出目录 | [实验入口说明](../experiments/comparison/README.md) |
| 七种运行时配置及消融解释 | [消融说明](../experiments/ablations/README.md) |

对照实验入口是宿主机仓库根目录的 `python run_experiment.py`。`main` 默认使用 SemantiST、AFL++、ICSQuartz，全部 90 个目标，每目标每工具 300 秒、5 次；`smoke` 使用三个代表目标，20 秒、1 次。用新 `--run-id` 和实际可用的 `--cpus` 启动，其他条件可显式覆盖。成功后自动导出逐次指标；比较和解释另行执行。

通用单目标流水线仍是安装后的 `semantist`，使用 `--st-file`、`--function`、`--run-dir`、`--fuzz-timeout`。它与比较入口的 `--fuzz-time`、共同 ASan 指标和输出目录不是同一套接口。`semantist-report` 属于通用报告流程，不能用其报告数量直接替代比较实验的 `paper_metrics`。

## 当前实现

| 内容 | 文档 |
| --- | --- |
| 模块边界和源码组织 | [Architecture](architecture.md) |
| 工具端到端技术说明 | [完整技术说明](semantist_tool_technical_reference_zh.md) |
| 调度、种子画像、任务预算、定向及周期变异 | [语义引导算法](stg_semantic_ranking_zh.md) |
| STG 静态模型、可信度与支持边界 | [静态语义模型](stg_semantic_model_zh.md) |
| STG 生成命令和产物 | [STG 使用说明](stg.md) |
| IR 探针、覆盖位图、周期和状态遥测 | [Semantic IR instrumentation](semantic_ir_instrumentation.md) |
| 方言归一化、依赖闭包和验证边界 | [Dialect normalization](dialect-normalization.md) |

默认 FB 周期上限为 256，engine、生成器和变异器共享该配置；文件状态 trace 默认关闭，共享内存反馈仍可用。当前实现属于有界启发式语义引导，不是完整符号执行或 PLC 物理过程模型。详细限制和不同入口的参数校验行为见对应技术说明。

## 迁移、历史审查与证据

| 文档 | 阅读范围 |
| --- | --- |
| [Layout migration](migration.md) | 旧目录/接口到当前布局的迁移参考 |
| [Restructure report](restructure-report.md) | 重构时的结果与验证；历史行数和测试数不表示当前规模 |
| [算法实现审查，2026-10-08](algorithm_review_20261008_zh.md) | 修复前反例与问题清单，开头说明后续处理状态 |
| [算法修订与验收记录，2026-10-08](algorithm_validation_20261008_zh.md) | 算法验收的条件、局部性能证据及后续接口回归补充 |
| [RuSTy DWORD 报告](rusty_dword_high_bit_miscompilation_report_zh.md) | 指定编译器版本下的历史问题，不代表最新上游状态 |
| [OSCAT 漏洞笔记](notes/oscat漏洞报告.txt) | 历史人工分析，不能当作当前工具已确认的漏洞清单 |

本地验收记录引用的 `artifacts/` 被 Git 忽略，干净 clone 不会自带日志或历史输入。验证数字必须连同测试范围、源码/镜像身份和原始记录一起解读；更新文档不等于重新执行全部实验。服务器是否可进入正式实验，以该服务器的构建、测试及短测结果为准。

后续修改参数或实现时，应同时核对实验预设（`experiments/comparison/run_full_comparison.py`）、指标定义（`experiments/comparison/lib/metrics.py`）、当前算法说明及本索引；不要通过改写历史报告来制造新的验证结果。
