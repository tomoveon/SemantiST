# 服务器实验操作手册

更新日期：2026-10-12。这里是当前操作流程；本地历史验证与目标服务器验收分开记录。本次最终 `post_run` 版本仅作源码修改与审阅，没有运行测试、烟测、编译或验证实验，尚无这一版本的运行验收结果。

日常流程：**固定源码与镜像 → 多核正式实验 → 离线分析**。使用仓库根目录的 `run_experiment.py`；Git、部署和结果解释由你手动完成。短测预设仍可按需使用，本次更新不要求先跑短测。

| 预设 | 工具 | 目标 | 每目标、每工具 |
| --- | --- | --- | --- |
| `smoke` | SemantiST、AFL++、ICSQuartz | 三个代表目标 | 20 秒 × 1 次 |
| `main`（默认） | 同上 | 全部 90 个目标 | 300 秒 × 5 次 |

显式参数覆盖预设。`--cpus` 决定不同 trial 的并发槽位，每个 trial 仍使用一个 worker、一个 CPU。运行器自动准备目标、记录环境，在每条试验停止并确认清理后读取原生统计和候选；每批结束保存已有 `metrics.csv` / `metrics.jsonl`，失败或无效试验也保留状态。结果表存在不等于实验通过完整性检查。

## 0. 本次更新：复用已构建的服务器镜像

本次采集改为 `collection_mode=post_run`：在线预算内不循环扫描统计、候选或日志，也不根据原生计数停止更新触发 watchdog。预算到期后停止并清理，再采集原生执行数、候选输入及元数据。无需重建基础环境镜像；新运行仍会重新准备各目标和 ICSQuartz 每目标镜像。以下命令供手动执行，本次修改没有执行这些操作。

在 Windows PowerShell 的仓库中，仅暂存本次文件；提交前阅读差异。如果当前不在 `main`，先按自己的分支流程合入 `main`，服务器下面固定获取 `origin/main`：

```powershell
Set-Location -LiteralPath 'D:\docker study\myfuzzer\SemantiST'
git status --short
git diff --stat
git add -- experiments/comparison/run_full_comparison.py experiments/comparison/adapters/base.py experiments/comparison/adapters/aflplusplus.py experiments/comparison/adapters/semantist.py experiments/comparison/adapters/icsfuzz.py experiments/comparison/adapters/icsquartz.py experiments/comparison/lib/process.py experiments/comparison/lib/metrics.py experiments/comparison/lib/schema.py experiments/comparison/metrics_schema.json experiments/comparison/analysis/compare_tools.py experiments/comparison/README.md docs/experiment_standard.md docs/server_experiment_runbook_zh.md tests/unit/test_icsquartz_lifecycle.py tests/unit/test_comparison_experiment.py tests/unit/test_independent_experiments.py tests/unit/test_experiment_runner_safety.py
git diff --cached
git commit -m 'Collect experiment metrics after trials stop'
git push origin main
git rev-parse HEAD
```

服务器上，先确保旧实验已停止、当前仓库工作树干净，再在仓库根目录执行。工作树不干净时先保存自己的改动，不使用强制重置：

```bash
git status --short
test -z "$(git status --porcelain)" && git fetch origin && git checkout --detach origin/main
git rev-parse HEAD
export CONTAINER_RUNTIME=podman
export SEMANTIST_IMAGE=30decb96791ef98e3c7bee7e37314ddb540fddfeda41568fd0d6f72272c96f6a
```

该镜像 ID 是本次用户服务器已有的 SemantiST 镜像，其他服务器须替换为自己的镜像 ID。确认上面的更新命令成功后，在原有宿主机 Python 环境中使用全新 ID 启动正式实验；这里沿用该服务器的 CPU `2-9`、300 秒和 5 次重复：

```bash
python3 -u run_experiment.py --experiment main --run-id "main_postrun_$(date +%Y%m%d_%H%M%S)" --cpus 2-9 --fuzz-time 300 --fuzz-trials 5 --container-runtime podman --semantist-image "$SEMANTIST_IMAGE"
```

本次指标合同是 `semantist.independent-tool-metrics/2`，不要将这些数据与旧 `/1` 轮询采集批次合并。所有工具使用停止后的原生快照，可能包含停止过程尾部的执行和候选；统一宿主机首次发现时间没有测量，`first_crash_time_seconds=null`。原生元数据中的时间另行保存并标明自己的起点，不冒充统一时间。原生执行数和候选仍然收集；缺失数据保留未知。

每批结束更新 `campaign-status.json` 的 `completed`、`failed`、`not_started` 列表以及 partial `raw-runs.jsonl`。已确认清理的单 trial 工具或采集失败会留下失败记录并继续后续批次；基础设施失败、无法确认清理或无法持久化结果时停止后续批次。最后存在无效或缺失记录会返回非零，已有指标仍可查看，且不会自动把有错矩阵聚合成成功结果。没有自动重试或恢复。

## 1. 本机保存源码并推送

当前开发容器把 Windows 的 `D:\docker study\myfuzzer` 绑定到容器内的
`/myfuzzer`，项目位于 `/myfuzzer/SemantiST`。两处看到的是同一个
Git 工作树，可以从 PowerShell 进入容器后提交；提交会同时写入宿主机的 `.git`。
如果容器中的源码只是镜像 `COPY` 进去的快照，则不能把容器当作唯一仓库，因为容器删除后提交也会丢失。

先在 Windows PowerShell 查看容器和挂载：

```powershell
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}'
docker inspect <开发容器ID或名称> --format '{{json .Mounts}}'
docker exec -it <开发容器ID或名称> bash
```

进入容器后执行；分支名可替换：

```bash
cd /myfuzzer/SemantiST
test "$(git rev-parse --show-toplevel)" = /myfuzzer/SemantiST
git status --short
git diff --stat
git diff --check

git switch -c codex/server-experiments-20261009
# 只暂存本次已审阅的具体文件；本次 post_run 更新使用第 0 节的文件清单。
# 在这里执行对应的 git add -- 具体文件列表，再继续提交。
git diff --cached --stat
git diff --cached --check
git diff --cached
git commit -m 'Prepare simplified server experiments'
git push -u origin HEAD
git status --short
git rev-parse HEAD
```

逐步检查命令是否成功。最后应是干净工作树；记录完整提交号，供服务器核对。
确认新增入口、消融文件和测试均已暂存；不要只推送旧 HEAD。`artifacts/`、旧语料、
`.venv/` 和编译产物不上传。如果容器没有 GitHub 凭据，可以在容器内完成
`git commit`，退出后到同一个 Windows 仓库执行 `git push -u origin HEAD`；bind mount
下这仍然是同一个提交。

## 2. 服务器首次准备、构建和测试参考

本节适用于首次部署或工具链变更，保留既有流程供以后使用。已有镜像且只同步本次实验脚本时使用第 0 节，不必重建镜像或执行本节测试。

使用 Linux x86_64、宿主机 Python 3.11+、Git、`taskset` 和可访问的 Podman。
支持 rootless Podman；运行器会为 bind mount 添加 SELinux relabel，并用 `taskset`
绑定 trial 的 CPU，避免依赖 rootless 环境中可能没有委派的 cpuset cgroup。首次构建需联网获取工具链。
实验编排器运行在服务器宿主机，自动启动容器；不需要手工进入容器逐个运行工具。
建议在 `tmux` 中执行，断开 SSH 后可重新连接原会话。

```bash
podman info
command -v taskset
uname -m
tmux new -s semantist
```

下面在同一个 Bash / tmux 会话中执行：

```bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

git -c core.autocrlf=false clone --branch codex/server-experiments-20261009 \
  https://github.com/tomoveon/SemantiST.git
cd SemantiST

# 替换为本机推送后的完整提交号。
REV='REPLACE_WITH_THE_40_CHARACTER_COMMIT'
git checkout --detach "$REV"
test "$(git rev-parse HEAD)" = "$REV"
test -z "$(git status --porcelain)"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python benchmarks/check_benchmark_files.py --check-files
mkdir -p artifacts/server-logs

IMAGE="semantist:exp-$REV"
podman build --pull --platform linux/amd64 \
  --build-arg "SEMANTIST_SOURCE_REVISION=$REV" -t "$IMAGE" . \
  2>&1 | tee artifacts/server-logs/build-semantist.log
python -u experiments/baselines/build_images.py aflplusplus icsquartz \
  --container-runtime podman 2>&1 | tee artifacts/server-logs/build-baselines.log

# 后续入口自动读取该变量；固定镜像 ID，避免标签被替换。
export SEMANTIST_IMAGE="$(podman image inspect "$IMAGE" --format '{{.Id}}')"
export CONTAINER_RUNTIME=podman
test "$(podman image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$REV"

podman run --rm "$SEMANTIST_IMAGE" python3 -m pytest -ra \
  2>&1 | tee artifacts/server-logs/pytest.log
podman run --rm "$SEMANTIST_IMAGE" cargo test --workspace --locked --jobs 1 \
  2>&1 | tee artifacts/server-logs/cargo-test.log
podman run --rm "$SEMANTIST_IMAGE" bash tests/e2e/semantic_ir_e2e.sh \
  2>&1 | tee artifacts/server-logs/semantic-ir-e2e.log
```

已有仓库时，在干净工作树中 `git fetch origin`，切换到指定提交即可；不强行重置未保存的改动。测试失败先修复，关键依赖缺失导致的跳过、E2E 退出码 77 不算通过。用新镜像本身的源码做测试，不要把宿主机整个仓库挂载到 `/work/SemantiST`。

默认三工具无需外部 ICSQuartz checkout；使用本仓库 fixture 和固定基线镜像。可选 ICSFuzz 的 CODESYS 工件与校准另见基线文档，不属于这里的默认流程。环境镜像只需构建一次；逐目标编译由运行入口完成。实验期间保持源码和镜像不变，避免同时进行重型构建。

### 此前 ASan 插桩修复的更新参考

以下是此前编译流程修复的独立验收步骤，不是本次 `post_run` 更新的前置条件，也未在本次执行。

公共 IR 准备步骤会根据 `SEMANTIST_TARGET_SANITIZERS`，为保留的 ST 函数添加并验证 `sanitize_address` 属性，随后由各工具的 Clang 完成 ASan 插桩。仅向已有 LLVM IR 传入 `-fsanitize=address` 或链接 ASan runtime，不能保证 ST 函数内的读写受保护。该步骤同时用于 SemantiST、AFL++ 和 ICSQuartz，独立于语义插桩开关。

这次修复只更新 Python 编译流程，无须重建已有 LLVM 21 / RuSTy 环境镜像。服务器在没有实验运行、工作树干净时更新到修复提交；若之前使用 detached HEAD，也可以继续固定提交：

```bash
git fetch origin
REV='REPLACE_WITH_THE_FIX_COMMIT'
git checkout --detach "$REV"

# 保留原来的 SEMANTIST_IMAGE 和 CONTAINER_RUNTIME，验证挂载的新源码。
"$CONTAINER_RUNTIME" run --rm \
  -v "$PWD:/repo:ro,z" \
  -w /repo -e PYTHONPATH=/repo -e PYTHONDONTWRITEBYTECODE=1 \
  "$SEMANTIST_IMAGE" python3 -m pytest -q -p no:cacheprovider \
  tests/unit/test_asan_preparation.py tests/integration/test_asan_instrumentation.py
```

测试必须实际通过，不能把缺少 LLVM 等依赖造成的 skip 当作验收通过。之后用新的 `--run-id` 运行实验，入口会重新生成目标 IR、对象和可执行文件，并构建 ICSQuartz 每目标镜像；不要复用修复前的目标准备目录或已运行的 trial。记录新源码提交及原环境镜像 ID，不把原镜像的 revision 标签当成此次源码版本。若以后修改 Rust fuzzer、STG 工具、RuSTy 补丁或依赖，则还需更新镜像中的预编译程序。

## 3. 选 CPU 并正式运行

先检查允许使用的 CPU 和内存：

```bash
lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE
python -c 'import os; print(sorted(os.sched_getaffinity(0)))'
free -h
df -h .

# 示例：必须改成服务器允许且空闲的核，尽量选不同物理核。
CPUSET='2-9'
CAMPAIGN="main_postrun_$(date +%Y%m%d_%H%M%S)"
```

`smoke` 仍提供三个代表目标、共 9 条短试验的可选预设，不是启动正式实验的必需步骤。本次不追加短测或长时间验证。当前采集模式没有计数停滞 watchdog；在线状态稀疏不会因为宿主机采集轮询而提前结束试验。

正式实验直接一条命令，默认 90 目标 × 3 工具 × 5 次，共 1350 条：

```bash
python -u run_experiment.py --experiment main \
  --run-id "${CAMPAIGN}_main" --cpus "$CPUSET" --container-runtime podman \
  2>&1 | tee "artifacts/server-logs/${CAMPAIGN}_main.log"
```

需要不同预算时加 `--fuzz-time 600 --fuzz-trials 10` 等参数，并使用新的批次名。只跑一个工具加 `--tools semantist`；选目标用可重复的 `--target-id`，它会替换预设目标。只查看排程时加 `--preflight-only`，但预检会占用该 ID，实际运行另取新 ID。

一个入口会先完成全部目标准备，再在同一 CPU 池调度在线 trial；多个工具共享池，总并发不会变成“工具数 × 核数”。8 核时，默认全部在线预算理论约 14.1 小时，另加编译、批次等待和清理开销。CPU 绑定不排除其他用户的任务；避免多个 runner 共享同一 CPU 池，并观察 `podman stats --no-stream`、内存和磁盘，再增加并发。给系统预留资源，不将同一物理核的 SMT 线程当成独立物理核。

## 4. 实验结束后分析

运行过程中每批结束就会导出已有逐 trial 指标；成功退出还要求支持目标完成预算、采集完整且原生计数为正。先查看 `campaign-status.json` 和 `metrics.csv` 的有效性与失败字段。静态不支持的目标仍在 `support-matrix.csv` 和逐次记录中标明，不等于有效试验。完整实验结束后需要跨工具汇总时再执行：

```bash
python experiments/comparison/analysis/compare_tools.py \
  --experiment-dir "artifacts/experiments/${CAMPAIGN}_main" \
  --out "artifacts/comparisons/${CAMPAIGN}_main"
```

生成 `comparison.csv` / `comparison.json` 和指标表。若分别运行了工具，可重复传入 `--experiment-dir`。只比较目标、预算、重复次数和配对种子一致的批次；默认 namespace 已在工具间配对，无需日常手动设置。

检查有效 trial、缺失指标和 `comparability_notes`。原生崩溃候选不等于独立漏洞；重放、根因去重、显著性分析和论文解释可以后做。不同工具的执行模型、首次发现与执行数口径见 [实验标准](experiment_standard.md)。

消融继续使用已有入口，例如以下为一个 FB 目标的多周期机制比较：

```bash
python experiments/ablations/run_ablation_suite.py \
  --run-prefix "${CAMPAIGN}_ablation" \
  --variants full,no_transition_guidance,no_prefix_growth,no_multicycle_guidance \
  --target-id scan_cycle_smart_oobw_1 --cpus "$CPUSET" \
  --container-runtime podman
```

这只是选目标的示例；正式数据集需提前确定。各配置顺序执行，配置内部 trial 并发，各目录自动输出指标。用 `configuration_id` 区分并手动分析，不混入主工具比较。更多配置见 [消融说明](../experiments/ablations/README.md)。

## 失败、结束与进一步检查

- 每次使用新 ID；已有目录直接拒绝，不自动覆盖或恢复。失败目录保留，修复后以新 ID 重跑选定组，不把失败原目录和重复的替代记录一起比较。
- SSH 断开先 `tmux attach -t semantist`，确认原任务是否仍在运行。Ctrl+C 可能等待当前批完成；构建中断后检查 `podman ps` 和构建进程，确认残留停止再重跑。
- 长实验可事先按不重叠的目标集合分组；不要事后悄悄删掉失败目标。运行中不递归改权限、不备份大文件，所有写入停止后再处理归属和备份。
- 在 Windows 本机烟测使用短路径，例如 `--artifact-root artifacts/i8 --run-id s4`。已有长路径导致 AFL++ 文件不可见的记录，不能通过取消计数检查绕过。
- 常用帮助：`python run_experiment.py --help`；高级参数：`--help-all`。默认周期上限 256、执行超时 1000 ms、关闭文件状态 trace；修改这些条件需在结果中明确区分。

本地历史版本先前通过了 9 条三工具 20 秒在线检查和 2 条 ICSQuartz 180 秒检查，证据在 `artifacts/i8/s3`、`artifacts/i8/p3`。这些不覆盖本次 `post_run` 修改，也不能替代目标服务器结果。算法验证另见 [验收记录](algorithm_validation_20261008_zh.md)。

此前预设入口简化后的 Linux 回归结果为 `176 passed, 112 subtests passed`，日志在 `artifacts/algorithm_validation_20261008/preset-final-tests.log`。其中检查了当时的 manifest 排程、参数覆盖、旧入口兼容和指标导出；这些历史记录不覆盖本次 `post_run` 变更。

确认任务结束后，连同源码提交号、镜像身份和原始工件备份结果：

```bash
tar -czf "../${CAMPAIGN}_artifacts.tar.gz" artifacts
sha256sum "../${CAMPAIGN}_artifacts.tar.gz" > "../${CAMPAIGN}_artifacts.tar.gz.sha256"
```

需要归档工具链时另用 `podman save` 保存环境镜像；它不包含宿主机实验结果。部署、源码保存、可选短测、正式实验和分析均可按上述阶段分别执行。
