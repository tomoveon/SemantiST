# SemantiST 改进用 100 秒测试集

这份固定的开发回归集依据 2026-11-06 完整实验的 `overall/full`、每目标五次 300 秒结果挑选，不作为论文总体发现率的样本。20 个目标分为：15 个 SemantiST 完成但未命中、基线有发现的目标（10 个 FUNCTION、5 个 FUNCTION_BLOCK）；1 个 SemantiST 五次引擎均以状态 139 退出的 `BUFFER_SEARCH`；1 个 SemantiST 4/5、两基线 5/5 的 `CRC_GEN`；3 个 SemantiST 原有多周期发现的回归哨兵。逐目标旧结果冻结在 [improvement_pilot.json](improvement_pilot.json)。

`PARSET` 和 `IRTRANS_DECODE` 的 ICSQuartz 原始 crash 元数据中存在 `scan_cycles > 1`，只说明执行跨周期，不能据此认定前序输入是触发所必需。`LIST_NEXT`、`SEQUENCE_64` 的 ICSQuartz 旧汇总虽是确认结果 0/5，却保存了原始 crash 文件；应同时查看原始事件，不能把汇总零值解释为它没有多周期行为。`BUFFER_SEARCH` 的 SemantiST 0/5 是运行失败，不是完成 300 秒仍未发现。

每个目标让 SemantiST、AFL++、ICSQuartz 各用正常适配器和各自 seed 运行一次，在线预算均为 100 秒、随机种子编号均为 0，共 60 个任务。旧实验中，17 个差距/失败目标的两个基线在 `s0` 均于 100 秒内首次命中；三个哨兵的 SemantiST `s0` 首次命中约为 50.9、32.4、1.8 秒。这使单轮检查能较快发现明显退化，但一次运行不能代表稳定的发现率。

在项目根目录执行。每次源码变化后使用新的带时间戳结果目录：

```bash
set -euo pipefail
IMAGE_TAG="semantist:improvement-pilot-$(date +%Y%m%d-%H%M%S)"
RUN_ROOT="$PWD/artifacts/experiment-pilots/improvement-$(date +%Y%m%d-%H%M%S)"
podman build --no-cache --format docker \
  -f benchmarks/experiments/Containerfile -t "$IMAGE_TAG" .
python3 -m fuzzer.cli.improvement_pilot --root "$RUN_ROOT"
python3 -m fuzzer.cli.experiment matrix --experiment overall --root "$RUN_ROOT"
python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment overall --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --llm-env-file "$PWD/.env.semantist-llm" --build-jobs 4 --plan-jobs 2
python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment overall --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --schedule standard --jobs 6
python3 -m fuzzer.cli.experiment summary --experiment overall --root "$RUN_ROOT"
```

必须指定 `--schedule standard`：现有 `target_waves` 只接受 1,500 项的完整正式矩阵。`overall` 和 `--allow-formal` 在这里仅用于复用现有运行器的筛选与时长门槛；pilot 结果目录不可与正式实验目录混用。并发 6 次时最多使用 12 个物理核；每项仍受现有的两核、8 GiB 限制。构建和 SemantiST LLM plan 冻结在 100 秒在线计时之前。清单和生成入口独立于正式 `selection.json` 与 `matrix.py`，不会改变正式实验矩阵。

检查结果目录的 `summary.json`、`per-target.json`：先看 60 项是否完成，再看有发现的目标、首次发现时间。SemantiST 还应看引擎 `metrics/engine-summary.json` 的序列和后缀 scan 吞吐，以及现有反馈事件中任务距离、前缀和 KLEE seed 的执行/保留情况。FUNCTION_BLOCK 要结合 ICSQuartz 原始 crash 文件与周期元数据解释确认结果。

做 A/B 改动时保持相同目标、seed、在线时长和两核预算；性能改善还要确认三个多周期哨兵没有退化。改动有希望后再增加重复种子，最终仍回到冻结的 100 目标正式矩阵评估。

