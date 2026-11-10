# SemantiST

SemantiST 面向 IEC 61131-3 Structured Text（ST），结合 LibAFL 原生模糊测试、源码语义目标图（STG）、
LLVM IR 探针和有界 KLEE 查询。可选的 LLM 在搜索前提出测试计划。
风险候选须经原生冷启动回放，才可能记为 finding。

## 构建镜像

推荐 Linux `x86_64`，使用 Podman 或 Docker。构建需要联网，建议预留至少
4 个 CPU、8 GiB 内存和 15 GiB 磁盘空间。镜像包含 RuSTy、LLVM、AFL++、
KLEE 和虚拟时钟运行环境。以下命令默认用 Podman；使用 Docker 时把第一行改成
`ENGINE=docker`，其余命令相同。

```bash
git clone https://github.com/tomoveon/SemantiST.git
cd SemantiST
ENGINE=podman
"$ENGINE" build --pull -t semantist:local .
```

## 配置 LLM（可选）

在仓库根目录执行 `nano .env.semantist-llm` 创建文件，填入服务地址、密钥和
模型名：

```dotenv
SEMANTIST_LLM_BASE_URL=https://<provider-api-host>/v1
SEMANTIST_LLM_API_KEY=<your-api-key>
SEMANTIST_LLM_MODEL=<model-id>
```

服务须支持 OpenAI 兼容的 `POST /v1/chat/completions` 接口和 JSON 格式响应。
`BASE_URL` 填到 `/v1` 为止；实际服务地址须从容器内可达。建议执行
`chmod 600 .env.semantist-llm`。运行时用 `--env-file` 把变量传入容器，

检查配置与连通性：

```bash
"$ENGINE" run --rm --env-file .env.semantist-llm semantist:local semantist-llm-check
```

## 复现单目标

下面以 OSCAT `INC2` 为例。这是旧的单工具预算示例，并非 100 目标正式主实验。
此示例的整个混合搜索阶段限时 600 秒，目标构建另有
600 秒上限；LLM 只在启用 `--llm` 时参与搜索前规划。

```bash
mkdir -p artifacts
"$ENGINE" run --rm --init --env-file .env.semantist-llm \
  --mount type=bind,src="$(pwd)/artifacts",dst=/work/SemantiST/artifacts \
  semantist:local \
  semantist \
    --st-file benchmarks/oscat/targets/oscat_basic_inc2/target.st \
    --function INC2 \
    --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
    --run-dir artifacts/runs/inc2-600s \
    --build-timeout 600 \
    --fuzz-timeout 600 --full-hybrid-duration --llm
```

不使用 LLM 时，去掉 `--env-file .env.semantist-llm` 和 `--llm` 即可运行混合搜索。
将 `--fuzz-timeout 600` 改为 `60` 可做短时检查；每次运行请使用新的
`--run-dir`。结果保存在 `artifacts/runs/inc2-600s/`，包括 STG、搜索任务、
语料、finding 和报告。`confirmed` 仅表示相同归一化源码、stub 和 Harness 下
的原生冷启动可重复，不自动代表厂商原始运行时也存在该风险。

更多目标和批量运行方法见 [OSCAT 基准](benchmarks/oscat/README.md)；
正式主实验见 [实验运行说明](benchmarks/experiments/README.md)：`overall`
共 1500 项（100 目标、三工具、每目标每工具 s0-s4），每项 300 秒在线墙钟；
完整去重比较矩阵共 3500 项，600 秒仅用于另一个预算比较。
先在同一个 `artifacts/experiments/overall-<时间戳>` 根目录构建三工具并冻结
100 个计划，再分三条命令运行 SemantiST、AFL++、ICSQuartz。工具分别使用
物理核 0-9、10-19、20-29，每项两核和 8 GiB；正式 SemantiST 保留在线
KLEE。结果根目录位于 `/home` 磁盘。失败项需查看日志并用同 root 的
`--retry-failed` 重跑；代码修改后的重测须换镜像和 root，不能混用旧 trial。

查看全部参数可运行：

`"$ENGINE" run --rm semantist:local semantist --help`。

## 测试与基准

```bash
"$ENGINE" run --rm semantist:local python3 -m pytest
"$ENGINE" run --rm semantist:local cargo test --workspace --locked --jobs 1
```

## 许可证

SemantiST 自有代码使用 [MIT License](LICENSE)。OSCAT 基准源码及提取目标是
第三方材料，遵循 [OSCAT 原始授权说明](https://github.com/eclipse-oscat/oscat-libs-archive#licenses)，
