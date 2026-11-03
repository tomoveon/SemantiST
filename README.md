# SemantiST

SemantiST 是一个面向 IEC 61131-3 Structured Text（ST，结构化文本）的语义引导模糊测试工具。它以 LibAFL 为执行引擎，通过编译器生成的语义目标图（Semantic Target Graph，STG）和 LLVM IR 插桩，将代码覆盖反馈与语义目标结合起来，用于测试 PLC 的 `FUNCTION` 和 `FUNCTION_BLOCK`。

项目支持标量 IEC 类型、`STRING`/`WSTRING`、`ARRAY`、`POINTER`，以及带状态、跨多个扫描周期执行的功能块。初始语料由有界、类型合法的语义种子生成器产生。可选的 LLM 规划在混合搜索前批量提出计划；计划须通过 ABI 校验和原生冷回放，搜索期间不再请求 LLM。实际发送的提示词和请求格式定义在 [LLM 计划代码](fuzzer/pipeline/plan_batches.py) 中。

SemantiST 第一版面向 ST 做语义引导混合测试：从已到达的扫描周期前缀出发，以原生变异和有界 KLEE 后缀求解探索剩余条件，并结合虚拟时间与快照处理历史相关缺陷。主流程位于 [fuzzer/pipeline](fuzzer/pipeline/)，下文描述当前实现及其边界。


## 目标驱动混合测试

`semantist` 默认运行混合测试：

```bash
semantist \
  --st-file benchmarks/defects/designed/d04_batch_queue/buggy/target.st \
  --function D04_BATCH_QUEUE \
  --run-dir artifacts/runs/d04 --fuzz-timeout 300
```

一次测试会：

1. 构建目标并写出 `bundle/execution-contract.json`、`abi.json`、`state-layout.json`、`goal-tasks.json`；
2. 选择性地在搜索前调用 LLM 生成并修订受 ABI 约束的测试计划，经原生冷回放筛选 seed；
3. 对适合符号执行的未解决任务做有界 KLEE 启动查询；
4. 用原生目标/状态/距离反馈持续搜索，并在引擎运行期间查询任务相关的多周期前缀；
5. 将 KLEE 后缀与前缀拼接、原生冷回放，并把有新颖性的非 finding seed 在线交给 fuzz 引擎继续执行；
6. 对风险候选做独立原生冷启动确认和最小化，写出 finding 与报告。

历史批次的版本、预算和结果只以各自 `artifacts/` 产物为准；短时 smoke、近似模型命中和生成了 seed 都不能单独证明完整目标覆盖、双向反馈或厂商原始语义下的漏洞。

旧覆盖引导引擎保留为基线：`--mode coverage`。

批量运行缺陷集：

```bash
semantist-batch --suite defects --variant both \
  --time-per-target 300 --out artifacts/batch/defects
```

批次每完成一个目标就更新 `batch-summary.json`/`.md`。中断后用相同参数加
`--resume` 继续；已记录目标直接复用，未完成目录会保留为 `*.interrupted-N` 后重跑。
新批次需使用空输出目录，避免覆盖旧证据。

单目标复现包：

```bash
semantist-replay artifacts/runs/d04/findings/confirmed/F0001
```

LLM 连通检查（不打印密钥）：

```bash
semantist-llm-check
```

`--llm` 和兼容开关 `--llm-initial-only` 均只在搜索前规划；默认最多 3 轮，可用
`--llm-plan-rounds 5` 设置为 5 轮。每轮将原计划、拒绝原因和原生回放结果交给
下一轮；仅有原生增益的计划进入初始 corpus。`--llm-strict-planning` 可让没有
有效计划的实验在准备阶段失败。`--llm-offline` 只读取已有缓存；缓存缺失时
记录失败状态并继续基线搜索。
配置读取环境变量，其次运行时源码根目录 `.env.semantist-llm`；容器内建议用
`podman run --env-file .env.semantist-llm ...` 注入，不要把密钥放进镜像。

定时器边界探针（输出两种 `timer_semantics` 的同格式 CSV）：

```bash
semantist-timer-probe --out artifacts/timer-probe
```

快速验证（推荐在 `localhost/semantist:0.1.0` 中）：

```bash
semantist-batch --suite defects --variant buggy --time-per-target 90 --out artifacts/batch/dev
semantist-batch --suite defects --variant fixed --time-per-target 60 --out artifacts/batch/fixed
```

## 工作流程

```text
厂商方言 ST
    │ intake：归一化、依赖闭包和兼容 provider
    ▼
RuSTy 兼容 ST + compatibility.json
    ├── RuSTy/LLVM IR ──► 类型化探针 ──► 原生目标 + KLEE 位码
    └── STG、ABI 与状态效果 ──► 目标任务、Harness、合法 seed
                                    │
                         搜索前 LLM 计划与冷回放
                                    │
                           启动 KLEE + 原生 fuzz
                                    │        ▲
                                    ▼        │ 回注新颖 seed
                          在线前缀 → KLEE 后缀
                                    │
                         冷回放、确认、最小化、报告
```

主要能力：

- 从 ST 程序中提取带稳定目标 ID 的 STG；
- 对 LLVM IR 进行语义插桩，并生成目标 POU 的 C Harness；
- 使用覆盖反馈和语义反馈调度语料；
- 对 ST 输入执行类型感知、结构化变异；
- 支持功能块多扫描周期输入及内部状态观测；
- 对发现去重、重放和最小化，输出 JSON 与 Markdown 报告。

## 仓库内容

`compiler/` 包含 intake、STG、IR 探针和 Harness；`fuzzer/` 包含原生引擎、
混合搜索、LLM 计划、回放与报告；`tests/` 是回归测试。本地 `docs/` 保存研究
和设计笔记，不随源码发布。`benchmarks/oscat/`
保存默认 100 项目标及清单，`benchmarks/defects/` 保存独立缺陷回归集。
`benchmarks/oscat_basic/source/` 和 `oscat/oscat/` 保存这些基准所引用的
OSCAT 源码导出。运行产物写入 `artifacts/`，本地环境配置保存在 `.env.*`；
这两类文件不会提交到仓库。

## 环境要求

推荐使用 Podman。当前主要复现环境为 Linux `x86_64`，需要：

- Podman；
- 建议至少 4 个 CPU、8 GiB 内存和 15 GiB 可用磁盘；
- 构建时能够访问 Ubuntu 软件源、LLVM 软件源、PyPI、crates.io 和 GitHub。

容器镜像包含 Python、Rust 1.90.0、LLVM 21、AFL++、固定提交并应用 SemantiST 补丁的 RuSTy、预编译的 IEC 标准库，以及三个 SemantiST release 可执行文件。主 `Dockerfile` 还会构建 LLVM 18、Z3、KLEE 和虚拟时钟标准库，因此它直接产出可运行混合测试的镜像。镜像构建需要联网；镜像构建完成后的本地 `mock` 运行不需要联网。

## 容器快速开始

以下命令使用 Podman。

### 构建镜像

```bash
git clone https://github.com/tomoveon/SemantiST.git
cd SemantiST
podman build --pull -t localhost/semantist:0.1.0 .
```

ARM 主机若需要复现 `x86_64` 环境，可使用：

```bash
podman build --platform linux/amd64 -t localhost/semantist:0.1.0 .
```

### 运行测试

```bash
podman run --rm localhost/semantist:0.1.0 python3 -m pytest

podman run --rm localhost/semantist:0.1.0 \
  cargo test --workspace --locked --jobs 1
```

### 构建示例目标

```bash
mkdir -p artifacts

podman run --rm \
  --mount type=bind,src="$(pwd)/artifacts",dst=/work/SemantiST/artifacts \
  localhost/semantist:0.1.0 \
  semantist \
    --st-file benchmarks/oscat/targets/oscat_basic_inc2/target.st \
    --function INC2 \
    --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
    --run-dir artifacts/runs/inc2-build-check \
    --build-only \
    --skip-report
```

### 运行 60 秒模糊测试

```bash
podman run --rm \
  --mount type=bind,src="$(pwd)/artifacts",dst=/work/SemantiST/artifacts \
  localhost/semantist:0.1.0 \
  semantist \
    --st-file benchmarks/oscat/targets/oscat_basic_inc2/target.st \
    --function INC2 \
    --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
    --run-dir artifacts/runs/inc2-60s \
    --fuzz-timeout 60
```

每次运行都应使用一个不存在或为空的 `--run-dir`。

只挂载 `artifacts/`，不要把宿主机整个仓库挂载到 `/work/SemantiST`；后者会用宿主机文件覆盖镜像中已经完成构建和自检的源码快照。容器内的 Cargo target 和 RuSTy 位于 `/opt`，因此挂载输出目录不会遮住预编译工具。

## 主要输出

运行结果保存在指定的 `artifacts/runs/<运行名>/` 中：

| 路径 | 内容 |
| --- | --- |
| `events.jsonl` | 流水线事件 |
| `stg/` | STG、运行时 ID 和 IR 映射 |
| `semantic-task-plan.json` | 语义任务计划 |
| `unified-corpus/` | 初始、生成和运行时语料 |
| `target-corpus/` | 等待或已经回放的目标语料 |
| `findings/` | 发现及其证据 |
| `vulnerability-report/` | JSON 与 Markdown 报告 |

重新生成报告：

```bash
semantist-report \
  --run-dir artifacts/runs/inc2-60s \
  --function INC2 \
  --st-file benchmarks/oscat/targets/oscat_basic_inc2/target.st
```

## 原生开发

原生环境至少需要 Python 3.11、Rust 1.90.0、LLVM/Clang 21、AFL++ 和 C/C++ 构建工具。仅安装 Python 包可执行：

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

LLVM、AFL++ 和 RuSTy 仍需另外准备，因此一般建议优先使用 Docker 或 Podman 容器。常用开发测试命令：

```bash
.venv/bin/python -m pytest
cargo test --workspace --locked --jobs 1
```

查看命令行参数：

```bash
semantist --help
semantist-report --help
semantist-minimize --help
```

## 基准程序

当前默认 benchmark 清单是 `benchmarks/oscat/manifest.json`：80 个 OSCAT Basic、
10 个 OSCAT Network、10 个 OSCAT Building 目标，包含此前指定的 27 个目标。
`COMP` 按 OSCAT Basic 的 `BUFFER_COMP` 运行。每个目标保留源码提取的
`target.st`；跨库目标另用 `normalized.st` 记录兼容转换。Harness 仍由 SemantiST
构建生成。外部 ICSFuzz/scan-cycle 样本仅保留缺陷回归使用的目标，
不计入默认 benchmark。

完整 OSCAT 批次每个目标的整个混合搜索阶段默认 600 秒，构建另有 600 秒上限；
这 100 项尚未完成正式批量验证：

```bash
./benchmarks/oscat/run_full.sh artifacts/batch/oscat-100-600s
```

脚本检测已有批次摘要时自动使用 `--resume`。第二个参数可修改每目标混合搜索秒数，
第三个参数可修改构建上限。Network/Building 的归一化模型仍需逐目标验证；
构建失败或符号模型不支持都必须分别报告，不能计作混合搜索已完成。详见
`benchmarks/oscat/README.md`。

## 许可证

SemantiST 自有代码使用 [MIT License](LICENSE)。基准中的 OSCAT 源码和
提取目标属于第三方材料，不适用本仓库的 MIT 授权；这些历史导出应按
[OSCAT 原始授权说明](https://github.com/eclipse-oscat/oscat-libs-archive#licenses)
使用，来源见 [OSCAT 网站](https://www.oscat.de/)。ICSQuartz 样本的许可证见
[benchmarks/external/icsquartz/LICENSE](benchmarks/external/icsquartz/LICENSE)。
