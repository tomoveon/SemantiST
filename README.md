# SemantiST

SemantiST 是一个面向 IEC 61131-3 Structured Text（ST，结构化文本）的语义引导模糊测试工具。它以 LibAFL 为执行引擎，通过编译器生成的语义目标图（Semantic Target Graph，STG）和 LLVM IR 插桩，将代码覆盖反馈与语义目标结合起来，用于测试 PLC 的 `FUNCTION` 和 `FUNCTION_BLOCK`。

项目支持标量 IEC 类型、`STRING`/`WSTRING`、`ARRAY`、`POINTER`，以及带状态、跨多个扫描周期执行的功能块。初始语料由有界、类型合法的语义种子生成器产生，不依赖大语言模型；LLM 仅可选用于润色漏洞报告。

## 工作流程

```text
厂商方言 ST
    │ 归一化与依赖解析
    ▼
RuSTy 兼容 ST + compatibility.json
    ├── RuSTy 编译 ──► LLVM IR 插桩 ──► Harness/目标程序
    └── 语义提取 ──► STG ──► 语义任务与类型合法种子
                                      │
                                      ▼
                 LibAFL 覆盖反馈、语义调度与结构化变异
                                      │
                                      ▼
                         发现、回放、最小化与漏洞报告
```

主要能力：

- 从 ST 程序中提取带稳定目标 ID 的 STG；
- 对 LLVM IR 进行语义插桩，并生成目标 POU 的 C Harness；
- 使用覆盖反馈和语义反馈调度语料；
- 对 ST 输入执行类型感知、结构化变异；
- 支持功能块多扫描周期输入及内部状态观测；
- 记录发现并生成 JSON、Markdown 和 HTML 报告。

## 环境要求

推荐使用 Docker。当前主要复现环境为 Linux `x86_64`，需要：

- Docker Engine 或 Docker Desktop；
- 建议至少 4 个 CPU、8 GiB 内存和 15 GiB 可用磁盘；
- 构建时能够访问 Ubuntu 软件源、LLVM 软件源、PyPI、crates.io 和 GitHub。

Docker 镜像包含 Python、Rust 1.90.0、LLVM 21、AFL++、固定提交并应用 SemantiST 补丁的 RuSTy、预编译的 IEC 标准库，以及三个 SemantiST release 可执行文件。镜像构建需要联网；镜像构建完成后的本地 `mock` 实验不需要联网。

论文对照实验使用的 AFL++、ICSQuartz、ICSFuzz 和 StructuredFuzzer 环境由
`experiments/baselines/` 中的独立 Dockerfile 构建。该目录只保存固定版本的
构建配方和校验值，不把第三方工具源码复制到 SemantiST 仓库中。当前阶段只
构建并验证基线环境，不运行正式实验；详见
[`experiments/baselines/README.md`](experiments/baselines/README.md)。

## Docker 快速开始

### 构建镜像

```bash
git clone https://github.com/tomoveon/SemantiST.git
cd SemantiST
docker build --pull -t semantist:0.1.0 .
```

ARM 主机若需要复现 `x86_64` 环境，可使用：

```bash
docker build --platform linux/amd64 -t semantist:0.1.0 .
```

### 运行测试

```bash
docker run --rm semantist:0.1.0 python3 -m pytest

docker run --rm semantist:0.1.0 \
  cargo test --workspace --locked --jobs 1
```

### 构建示例目标

```bash
mkdir -p artifacts

docker run --rm \
  --mount type=bind,src="$(pwd)/artifacts",dst=/work/SemantiST/artifacts \
  semantist:0.1.0 \
  semantist \
    --st-file benchmarks/oscat_basic/functions/inc2/target.st \
    --function INC2 \
    --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
    --run-dir artifacts/runs/inc2-build-check \
    --build-only \
    --skip-report
```

### 运行 60 秒模糊测试

```bash
docker run --rm \
  --mount type=bind,src="$(pwd)/artifacts",dst=/work/SemantiST/artifacts \
  semantist:0.1.0 \
  semantist \
    --st-file benchmarks/oscat_basic/functions/inc2/target.st \
    --function INC2 \
    --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
    --run-dir artifacts/runs/inc2-60s \
    --fuzz-timeout 60 \
    --report-llm-provider mock
```

每次运行都应使用一个不存在或为空的 `--run-dir`。`mock` 报告器完全离线。

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
| `vulnerability-report/` | JSON、Markdown 和 HTML 报告 |

重新生成报告：

```bash
semantist-report \
  --run-dir artifacts/runs/inc2-60s \
  --function INC2 \
  --st-file benchmarks/oscat_basic/functions/inc2/target.st \
  --llm-provider mock
```

## 原生开发

原生环境至少需要 Python 3.11、Rust 1.90.0、LLVM/Clang 21、AFL++ 和 C/C++ 构建工具。仅安装 Python 包可执行：

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

LLVM、AFL++ 和 RuSTy 仍需另外准备，因此一般建议优先使用 Docker。常用开发测试命令：

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

`benchmarks/external/manifest.json` 是统一的基准索引，当前包含：

- 61 个 OSCAT Basic 目标；
- 17 个 ICSFuzz 血缘目标；
- 12 个扫描周期目标。

## 许可证

SemantiST 主仓库使用 [MIT License](LICENSE)。第三方基准可能带有各自的许可证，使用或重新分发前请检查 `benchmarks/external/` 中的相关文件。
