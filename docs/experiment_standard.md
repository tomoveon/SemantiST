# AFL++、ICSFuzz 与 SemantiST 全量目标支持性与共同支持子集实验脚本规范

> 文档目标：本文是后续 agent 生成完整实验脚本、runner、adapter、schema、分析脚本和验收脚本的唯一依据。
>
> 实验对象：`semantist_full`、`aflplusplus_full`、`icsfuzz_full`。
>
> 实验范围：`/myfuzzer/SemantiST/benchmarks/external/manifest.json` 中的 90 个目标，包含 `FUNCTION` 与 `FUNCTION_BLOCK`。
>
> 固定预算：每个目标、每个工具、每个 trial 的在线 fuzzing 时间为 300 秒；每个目标、每个工具执行 5 个独立 trials。
>
> 运行矩阵规模：`3 tools x 90 targets x 5 trials = 1350` 条权威 run records。矩阵中的 unsupported、preprocessing failed、online start failed 也必须生成权威 run record。
>
> 实验性质：本文定义的是三工具对 90 个目标的全量支持性尝试、支持率报告，以及共同支持目标子集上的 fuzzing 对比；本文不声称三个工具都在 90 个目标上实际完成 fuzzing。

## 1. 固定输入与版本依据

### 1.1 工作目录

所有脚本在下列路径约束下运行：

```text
workspace_root = /myfuzzer
semantist_root = /myfuzzer/SemantiST
icsfuzz_root = /myfuzzer/ICSFuzz
icsquartz_root = /myfuzzer/ICSQuartz
paper_icsquartz = /myfuzzer/papers/ICSQuartz.pdf
benchmark_manifest = /myfuzzer/SemantiST/benchmarks/external/manifest.json
```

脚本不得通过扫描目录推断 benchmark 集合。脚本必须读取 `benchmark_manifest`，并校验下列 SHA-256：

```text
d9d12e1b2d2d406e5304745679bae0bf7c08b5a6afaba470607f2ece25976079  /myfuzzer/SemantiST/benchmarks/external/manifest.json
edaf8c3df99e3b23ab3ea4ce303a8cb2bc664207023730aec6c870d03831a262  /myfuzzer/papers/ICSQuartz.pdf
e8ba6f5683207b4ee6a73e1739fd6adaaeb0f3875bda8c2fc6c64a554e17ae72  /myfuzzer/ICSQuartz/run_experiment.py
d78e72690e8dc06c24714a660b264f34be927a58d011b45f3c70e7874cbc5fb4  /myfuzzer/ICSQuartz/src/experiments.py
60086385313ecf16e2def1590c61a845c4624f4c3f3a5e689554e315f4d4d063  /myfuzzer/ICSQuartz/README.md
```

本地源码版本必须写入 `environment.json`：

```text
SemantiST commit = 5ae1b74d064d303ecb7fc5aa70bf85f944b36907
ICSFuzz commit = 4758eaac1e62da56b7e9fadbbe110700c3af3480
ICSQuartz commit = 8021bd44f47147776c6394e008bbea23ac993076
```

当实际提交与上列提交不同，脚本仍执行，但 `environment.json` 必须记录实际提交、dirty file 列表和 dirty diff SHA-256。

### 1.2 ICSQuartz 实验约定的采纳项

本文采用 ICSQuartz 论文与源码中的下列实验组织方式：

- build/preprocessing 阶段与 fuzzing 阶段分开计时；
- fuzzing 参数包含 `fuzz_time`、`fuzz_trials`、`cpus` 和 `experiment`；
- 运行队列按 `fuzzer x benchmark x trial` 展开；
- 并发批大小等于可用 CPU 核数；
- 结果目录保存 raw stats 与 per-benchmark / overall summaries；
- 关键性能指标包含 `execs_per_sec`、`execs_total`、`first_crash_time`、`first_crash_executions`；
- CPU affinity 固定；
- crash 次数、首次 crash 时间、首次 crash 执行数分列；
- scan-cycle 目标保留 state/stale 相关指标。

## 2. Benchmark 权威集合

### 2.1 Manifest 校验

`benchmark_manifest` 必须满足：

```text
schema_version == semantist.external-benchmarks/1.0.0
target_count == 90
type(targets) == array
len(targets) == 90
len(targets) == target_count
```

每个 target 必须包含：

```text
suite
id
kind
function
st_file
upstream_repo
upstream_commit
upstream_path
upstream_sha256
lineage
transformations
```

带有 `compatibility_manifest` 的 target 必须使用该字段；不带该字段的 target 不传 compatibility manifest。

### 2.2 Suite 计数

脚本必须校验下列 suite 计数：

```text
oscat_basic = 61
icsquartz_icsfuzz = 17
icsquartz_scan_cycle = 12
total = 90
```

### 2.3 目标身份

脚本生成 `experiment-manifest.json` 时，必须为每个目标写入下列字段：

```json
{
  "target_id": "icsfuzz_bf_mmove_1",
  "suite": "icsquartz_icsfuzz",
  "kind": "FUNCTION",
  "function": "STFZ_ICSF_ICSFUZZ_BF_MMOVE_1",
  "st_file": "/myfuzzer/SemantiST/{manifest.st_file}",
  "st_file_sha256": "{sha256(st_file)}",
  "compatibility_manifest": "/myfuzzer/SemantiST/{manifest.compatibility_manifest}",
  "compatibility_manifest_sha256": "{sha256(compatibility_manifest)}",
  "upstream_repo": "{manifest.upstream_repo}",
  "upstream_commit": "{manifest.upstream_commit}",
  "upstream_path": "{manifest.upstream_path}",
  "upstream_sha256": "{manifest.upstream_sha256}",
  "ground_truth_status": "known_fault",
  "tool_support": {
    "semantist_full": "pending",
    "aflplusplus_full": "pending",
    "icsfuzz_full": "pending"
  }
}
```

target 不带 `compatibility_manifest` 时，`experiment-manifest.json` 中的 `compatibility_manifest` 与 `compatibility_manifest_sha256` 必须写为 `null`。

`icsquartz_icsfuzz` 与 `icsquartz_scan_cycle` 目标的 `ground_truth_status` 固定为 `known_fault`。`oscat_basic` 目标固定为 `unknown_ground_truth`，但已知 CVE 或已知 RuSTy compiler bug 由 replay/triage 阶段填充 fault metadata。

## 3. 固定实验参数

### 3.1 实验 ID 与矩阵

默认实验 ID 固定为：

```text
comparison_support_90targets_common_subset_300s_5trials
```

矩阵生成规则：

```text
tools = [semantist_full, aflplusplus_full, icsfuzz_full]
trials = [1, 2, 3, 4, 5]
targets = benchmark_manifest.targets in manifest order
rows = cartesian_product(tools, targets, trials)
expected_run_records = 1350
```

本文固定使用下列目标集合名称：

```text
all_90_attempted = benchmark_manifest.targets
common_supported_targets = targets whose three tool_support values are supported
semantist_aflpp_supported_targets = targets whose semantist_full and aflplusplus_full support values are supported
```

`all_90_attempted` 只表示三工具都必须尝试建立 run record。三工具横向 fuzzing 主结论只能基于 `common_supported_targets`。`all_90_attempted` 的结果只能用于支持率、稳定性、preprocessing 成功率和 unsupported 原因报告。

按本文第 4.3.1 节的 ICSFuzz support 判定，在本规范校验的输入树中 `icsfuzz_full` 支持 35 个目标，不支持 55 个目标。对应的 `icsfuzz_full` unsupported trial records 数为 `55 x 5 = 275`。runner 必须从实际路径重新计算该数量，并把计算结果写入 `support.json`；summary 标题不得把本实验称为“三工具 90 目标全量实际 fuzzing 对比”。

每个 row 的稳定 key：

```text
run_key = "{experiment_id}|{tool}|{suite}|{target_id}|trial_{trial_id:02d}"
```

### 3.2 RNG seed

每个 row 的 `rng_seed` 由下列算法生成：

```text
digest = sha256(run_key UTF-8 bytes)
rng_seed = (int(digest[0:16], 16) % 2147483646) + 1
```

`rng_seed` 必须传入支持显式 seed 的工具。工具不支持显式 seed 时，adapter 必须记录：

```json
{
  "rng_seed_requested": 123,
  "rng_seed_applied": null,
  "rng_seed_status": "tool_missing_seed_control"
}
```

该状态不改变 run 的执行资格，但 summary 必须单独报告 seed-control 缺失的 run 数。

### 3.3 时间预算

```text
online_budget_seconds = 300
trial_count = 5
per_execution_timeout_ms = 1000
afl_timeout_arg = 1000+
semantist_semantic_task_budget = 1000
semantist_fb_max_cycles = 10000
semantist_fb_stale_threshold = 0
semantist_fb_state_trace = true
semantist_report_llm_provider = mock
```

`per_execution_timeout_ms` 对 SemantiST 和 AFL++ 固定为 1000 ms。ICSFuzz 的 CODESYS scan-cycle 执行模型不接受 per-testcase timeout 参数；ICSFuzz adapter 必须记录 `per_execution_timeout_ms=null` 与 `execution_timeout_model="codesys_scan_cycle"`。

### 3.4 CPU 与并发

runner CLI 固定为：

```bash
python3 experiments/comparison/run_full_comparison.py \
  --experiment-id comparison_support_90targets_common_subset_300s_5trials \
  --manifest /myfuzzer/SemantiST/benchmarks/external/manifest.json \
  --artifact-root /myfuzzer/artifacts/experiments \
  --cpus 1-8
```

`--cpus` 接受逗号与区间格式。runner 同时运行的 online rows 数量等于解析出的 CPU 数量。每个 online row 绑定一个 CPU。runner 不在同一 CPU 上同时运行两个 online rows。

运行顺序使用确定性轮转：

```text
sort_key = sha256("{experiment_id}|schedule|{tool}|{suite}|{target_id}|{trial_id}")
```

runner 按 `sort_key` 升序调度 rows。该顺序写入 `schedule.jsonl`。

## 4. 工具全功能身份

### 4.1 `semantist_full`

`semantist_full` 包含下列 SemantiST 原生能力：

- RuSTy ST 编译；
- STG 提取；
- semantic IR 插桩；
- C harness 生成；
- AFL-compatible edge instrumentation；
- AddressSanitizer 与 UndefinedBehaviorSanitizer；
- STG bounded semantic initial generator；
- semantic target-aware scheduler；
- semantic typed mutation；
- cycle-aware mutation；
- FUNCTION_BLOCK 多周期执行；
- FUNCTION_BLOCK state trace；
- state-signature novelty；
- semantic objective；
- crash、signal、OOM、timeout、sanitizer finding；
- mock reporter 生成结构化报告。

`semantist_full` 不关闭 STG semantic initial generator，不关闭 semantic scheduler，不关闭 semantic mutation，不关闭 cycle-aware mutation，不关闭 sanitizer。

#### 4.1.1 SemantiST preprocessing

SemantiST preprocessing 为每个 `(tool, target)` 执行一次，并生成只读 preprocessing artifact。正式脚本禁止直接调用 `python3 -m fuzzer.pipeline.core --build-only` 作为 preprocessing 命令，因为该源码路径会在计时前生成并摄入 STG generated seeds。

SemantiST preprocessing 必须使用低层构建步骤，命令模板：

```bash
cd /myfuzzer/SemantiST
env -u GENERATED_SEED_DIR \
SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
SEMANTIST_STG_DIR="{preprocess_dir}/stg" \
OUT_LL="{preprocess_dir}/target.ll" \
OUT_OBJ="{preprocess_dir}/target.o" \
OUT_BIN="{preprocess_dir}/fuzz_target" \
HARNESS_OUT="{preprocess_dir}/harness.c" \
{compatibility_env} \
./compiler/scripts/compile_st.sh "{absolute_st_file}" "{function}"

cd /myfuzzer/SemantiST
env -u GENERATED_SEED_DIR \
SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
SEMANTIST_STG_DIR="{preprocess_dir}/stg" \
OUT_LL="{preprocess_dir}/target.ll" \
OUT_OBJ="{preprocess_dir}/target.o" \
OUT_BIN="{preprocess_dir}/fuzz_target" \
HARNESS_OUT="{preprocess_dir}/harness.c" \
{compatibility_env} \
./compiler/scripts/build_target.sh "{absolute_st_file}" "{function}"

cd /myfuzzer/SemantiST
python3 -c 'from pathlib import Path; from fuzzer.semantic.planning import generate_semantic_task_plan; generate_semantic_task_plan(Path("{preprocess_dir}/stg/stg-model.json"), Path("{preprocess_dir}/stg/stg-runtime-ids.json"), Path("{preprocess_dir}/semantic-task-plan.json"))'
```

`compatibility_env` 固定为：

```text
target has compatibility_manifest: SEMANTIST_COMPATIBILITY_MANIFEST="{absolute_compatibility_manifest}"
target lacks compatibility_manifest: delete the whole command line whose only payload is {compatibility_env} plus the trailing backslash
```

preprocessing 产物必须包含：

```text
{preprocess_dir}/stg/stg-model.json
{preprocess_dir}/stg/stg-runtime-ids.json
{preprocess_dir}/semantic-task-plan.json
{preprocess_dir}/fuzz_target
{preprocess_dir}/harness.c
{preprocess_dir}/base-seeds/
```

`target_name(function)` 的生成规则固定为：将 `function` 转为小写，将非 `[A-Za-z0-9_.-]` 字符替换为 `_`，删除首尾 `_`；空结果替换为 `target`。

`base-seeds/` 只允许包含 `sorted({target_st_file_parent}/seeds/*.seed)` 中的基础 Harness seed。目标没有基础 seed 时，adapter 创建一个文件名为 `seed`、内容为 `X,INT,0\n` 的 fallback 基础 seed。

preprocessing 阶段不得保存、复制或引用下列路径：

```text
{preprocess_dir}/generated-seeds/
{preprocess_dir}/unified-corpus/seeds/  # pipeline --build-only 的摄入结果
```

adapter materialize trial 时只能把 `base-seeds/` 复制到 `{run_dir}/unified-corpus/seeds/`。复制后的 online run directory 不得与其他 trial 共享可写文件。

adapter materialize trial 时还必须复制：

```text
{preprocess_dir}/fuzz_target -> {run_dir}/tool-artifacts/fuzz_target
{preprocess_dir}/harness.c -> {run_dir}/tool-artifacts/harness.c
{preprocess_dir}/stg/ -> {run_dir}/stg/
{preprocess_dir}/semantic-task-plan.json -> {run_dir}/semantic-task-plan.json
```

SemantiST online command 中的 `{target_bin}` 固定为 `{run_dir}/tool-artifacts/fuzz_target`。

trial 启动前必须执行断言：

```text
not exists({preprocess_dir}/generated-seeds)
not exists({run_dir}/generated-seeds)
not exists({run_dir}/build/generated-seeds)
{run_dir}/unified-corpus/seeds contains only files copied from {preprocess_dir}/base-seeds
```

任何断言失败时，该 row 写 `run_status="runner_error"`，`failure_reason.code="semantist_generated_seed_before_t0"`。所有依赖 STG 的 generated seed 必须由 Rust engine 在 `t0_monotonic_ns` 之后生成、执行和摄入。

#### 4.1.2 SemantiST online command

SemantiST online 阶段直接启动 Rust fuzzer engine。runner 在 `Popen` 前记录 `t0_monotonic_ns`。

环境变量模板：

```text
CARGO_TARGET_DIR=/myfuzzer/SemantiST/artifacts/cargo-target
SEMANTIST_SEMANTIC_TRACE_FILE={run_dir}/semantic-coverage.jsonl
SEMANTIST_SEMANTIC_RUNTIME_IDS={run_dir}/stg/stg-runtime-ids.json
SEMANTIST_FB_MAX_CYCLES=10000
SEMANTIST_FB_STALE_THRESHOLD=0
SEMANTIST_FB_STATE_TRACE=1
SEMANTIST_FB_STATE_TRACE_FILE={run_dir}/tool-artifacts/fb-state-trace.log
```

命令模板：

```bash
cd /myfuzzer/SemantiST
cargo run -p semantist --release -- \
  --target "{target_bin}" \
  --function "{function}" \
  --run-dir "{run_dir}" \
  --semantic-task-budget 1000 \
  --semantic-task-plan "{run_dir}/semantic-task-plan.json" \
  --semantic-model "{run_dir}/stg/stg-model.json" \
  --timeout-ms 1000
```

Semantic initial generator 在 engine 启动后执行；它产生、执行并筛选的输入属于在线预算。`semantic_generation` 阶段触发的 objective 或 sanitizer finding 计入 `semantist_full`。

### 4.2 `aflplusplus_full`

`aflplusplus_full` 是传统覆盖引导基线，使用相同 ST 源、相同 POU、相同 RuSTy 编译链、相同 C harness、相同 sanitizer、相同 AFL coverage instrumentation。它不使用 STG、semantic IR、semantic task plan、semantic initial generator、semantic scheduler、semantic mutation、cycle-aware semantic mutation。

#### 4.2.1 AFL++ preprocessing

AFL++ preprocessing 为每个 `(tool, target)` 执行一次。命令模板：

```bash
cd /myfuzzer/SemantiST
SEMANTIST_SEMANTIC_INSTRUMENTATION=off \
OUT_LL="{preprocess_dir}/target.ll" \
OUT_OBJ="{preprocess_dir}/target.o" \
OUT_BIN="{preprocess_dir}/fuzz_target" \
HARNESS_OUT="{preprocess_dir}/harness.c" \
./compiler/scripts/compile_st.sh "{absolute_st_file}" "{function}"

cd /myfuzzer/SemantiST
SEMANTIST_SEMANTIC_INSTRUMENTATION=off \
OUT_LL="{preprocess_dir}/target.ll" \
OUT_OBJ="{preprocess_dir}/target.o" \
OUT_BIN="{preprocess_dir}/fuzz_target" \
HARNESS_OUT="{preprocess_dir}/harness.c" \
./compiler/scripts/build_target.sh "{absolute_st_file}" "{function}"
```

带有 compatibility manifest 的 target，两个命令都必须增加：

```text
SEMANTIST_COMPATIBILITY_MANIFEST={absolute_compatibility_manifest}
```

AFL++ seed corpus 固定为：

```text
source seed files = sorted({target_st_file_parent}/seeds/*.seed)
fallback seed file = seed with bytes "X,INT,0\n"
```

存在 source seed files 时，不创建 fallback seed。不存在 source seed files 时，创建 fallback seed。AFL++ 不接收 SemantiST generated seeds。

#### 4.2.2 AFL++ online command

runner 在 `Popen` 前记录 `t0_monotonic_ns`。命令模板：

```bash
afl-fuzz \
  -i "{run_dir}/inputs" \
  -o "{run_dir}/tool-artifacts/afl" \
  -s "{rng_seed}" \
  -t 1000+ \
  -- \
  "{preprocess_dir}/fuzz_target"
```

AFL++ deterministic stage、calibration、havoc、splice、power schedule、queue management 全部属于 AFL++ 原生能力，并计入在线预算。

### 4.3 `icsfuzz_full`

`icsfuzz_full` 必须运行真实 ICSFuzz 工具，不得用 SemantiST、AFL++、ICSQuartz 的结果代替。

`icsfuzz_full` 的依据：

```text
/myfuzzer/ICSFuzz
/myfuzzer/ICSQuartz/src/fuzzers/icsfuzz.py
/myfuzzer/ICSQuartz/fuzzers/icsfuzz/start-fuzz.sh
/myfuzzer/ICSQuartz/scripts/calibrate-codesys.sh
```

ICSFuzz 固定运行条件：

```text
ASLR = disabled, /proc/sys/kernel/randomize_va_space == 0
CODESYS calibration file = /myfuzzer/ICSQuartz/.config/codesys-area-zero
container capabilities = SYS_NICE,SYS_PTRACE
scan_cycle_ms = 35
ICSFuzz native initial value = 0xdeadbeef
```

本规范校验的 `/myfuzzer/ICSFuzz` 与 `/myfuzzer/ICSQuartz/fuzzers/icsfuzz/` 中的 ICSFuzz 实现不读取 `SEED` 环境变量，变异流程不得记录为受 `rng_seed` 控制。ICSFuzz 每个 run 的 RNG 字段固定为：

```json
{
  "rng_seed_requested": "{rng_seed}",
  "rng_seed_applied": null,
  "rng_seed_status": "tool_missing_seed_control"
}
```

adapter 在 `command.json` 中保存 `SEED={rng_seed}` 时，该字段只表示 runner 请求痕迹；`run-result.json` 不得把 ICSFuzz 的 `rng_seed_status` 写成 `applied`。

#### 4.3.1 ICSFuzz support 判定

ICSFuzz adapter 对每个 target 执行 support 判定，判定输入固定为：

```text
/myfuzzer/ICSQuartz/benchmarks/{target_id}/codesys/
/myfuzzer/ICSQuartz/benchmarks/{target_id}/icsfuzz/
/myfuzzer/ICSQuartz/benchmarks/{target_id}/icsfuzz/harness.env
```

三项全部存在时，`support_status="supported"`。任一项不存在时，`support_status="unsupported"`，`failure_reason.code="icsfuzz_missing_codesys_layout"`，该 target 的 5 个 ICSFuzz trial 全部生成 `run-result.json`，`run_status="unsupported"`，`online_elapsed_seconds=0`，`finding_found=false`。

adapter 不生成 CODESYS 工程，不猜测 target offset，不猜测 target size，不用 SemantiST harness 替代 ICSFuzz 的内存注入模型。

#### 4.3.2 ICSFuzz preprocessing

supported target 使用 ICSQuartz 的 CODESYS/ICSFuzz Docker 流程构建。preprocessing 必须完成：

- 校验 ASLR 为 0；
- 校验 `.config/codesys-area-zero` 存在；
- 构建 CODESYS image；
- 构建 target CODESYS artifact；
- 构建 ICSFuzz container image；
- 保存 build logs；
- 保存 target offset、target size、CODESYS area zero、scan cycle ms。

#### 4.3.3 ICSFuzz online command

runner 在 container 启动前记录 `t0_monotonic_ns`。online command 使用 `/myfuzzer/ICSQuartz/fuzzers/icsfuzz/start-fuzz.sh` 的语义：

- 启动 CODESYS；
- 等待 CODESYS 初始化；
- 定位 CODESYS PID 与 PLC task TID；
- 运行 `./fuzzer {CODESYS_PID} {TARGET_ADDR} {TARGET_SIZE} {MAINTASK_TID}`；
- 监控 CODESYS crash；
- crash 后重启 CODESYS 与 fuzzer；
- 直到 runner 达到 300 秒预算并终止整个 container/process group。

ICSFuzz 的 `first_crash_time` 使用第 5.3 节的 runner-observed finding elapsed，不从 `icsfuzz.log` 第一行时间戳计算。`icsfuzz.log` 中的时间戳只用于辅助恢复 execution ordinal。

## 5. 时间计量标准

### 5.1 Preprocessing 时间

preprocessing 时间从 adapter 执行第一条 build/static preparation 命令前开始，到所有 online 可读 artifact 完成后结束。使用 `time.monotonic_ns()`。

写入字段：

```text
preprocessing_start_monotonic_ns
preprocessing_end_monotonic_ns
preprocessing_time_seconds
preprocessing_status
```

preprocessing 不执行 fuzzer 主循环，不执行 AFL++ calibration，不执行 ICSFuzz input mutation loop，不执行 SemantiST engine。

### 5.2 Online 时间

online 时间从 runner 即将 `Popen` online command 或启动 online container 前开始：

```text
t0_monotonic_ns = time.monotonic_ns() immediately before Popen/start_container
```

下列内容全部计入在线预算：

- fuzzer 进程启动；
- container 启动；
- forkserver 启动；
- corpus 加载；
- calibration；
- SemantiST semantic initial generation；
- AFL++ deterministic/havoc/splice；
- ICSFuzz CODESYS 启动等待；
- ICSFuzz 原生初始化；
- target testcase 执行；
- crash restart；
- fuzzer 内部日志 flush。

runner 在 `t0 + 300 秒` 终止 online process group/container。终止顺序固定为：

```text
SIGTERM
wait 5 seconds
SIGKILL
```

runner 发出的 SIGTERM/SIGKILL 不计为 target crash。

### 5.3 Finding 观察时间

首次发现时间由 runner 统一打点。工具日志时间、文件修改时间、Unix 时间和 sanitizer 报告中的时间不得作为主指标。

每个 adapter 必须实现轻量增量接口：

```text
scan_finding_events(run_dir, adapter_state) -> normalized finding candidates
```

`scan_finding_events` 只能返回已完整写入、可解析、带稳定 `candidate_id` 的 finding candidate。adapter 必须保存增量解析状态；日志型来源保存 byte offset，目录型来源保存已见文件 inode/path/hash，JSONL 来源保存已读 offset。

runner 在 `t0_monotonic_ns` 之后立即启动 finding observer loop。observer loop 对每个正在运行的 row 调用 `scan_finding_events`，轮询间隔固定满足：

```text
observer_poll_interval_ms <= 10
```

当 runner 第一次观察到某 row 的新 `candidate_id` 时，必须立即调用：

```text
observed_monotonic_ns = time.monotonic_ns()
first_finding_elapsed_seconds = (observed_monotonic_ns - t0_monotonic_ns) / 1_000_000_000
```

`observed_monotonic_ns` 的调用必须发生在去重、回放、stack normalization 和 summary 聚合之前。首个 candidate 的 `first_finding_elapsed_seconds` 写入 `first_common_finding` 或 `first_semantic_objective`。同一 row 后续 candidate 也必须保存各自的 `observed_monotonic_ns` 与 `observed_elapsed_seconds`。

adapter 的工具特定来源固定为：

```text
semantist_full: {run_dir}/findings/**/*.json and {run_dir}/events.jsonl
aflplusplus_full: {run_dir}/tool-artifacts/afl/default/crashes/id:* and hangs/id:*
icsfuzz_full: {run_dir}/tool-artifacts/wrapper.log lines containing "Crash detected"
```

adapter 保存工具自带时间字段时，字段名固定为：

```text
tool_reported_time_seconds
tool_reported_unix_time_seconds
tool_reported_execution_ordinal
```

这些字段只能作为辅助字段，不得覆盖 `observed_elapsed_seconds`。SemantiST startup、initial ingress、semantic generation 或 AFL++ calibration 中产生的 finding 均按 runner 首次观察时间计时，不得被压成 0 秒。

### 5.4 提前退出

online command 在 300 秒前退出时，runner 不重启该 row。状态按下列规则写入：

```text
returncode == 0 and finding_found == true  -> early_completed_with_finding
returncode == 0 and finding_found == false -> early_completed_no_finding
returncode != 0 and tool process crashed   -> online_crashed
returncode != 0 and startup failed         -> online_start_failed
```

提前退出的 `online_elapsed_seconds` 写真实值。summary 中的等时覆盖率只使用 `run_status="budget_completed"` 的 row。

## 6. Run 状态枚举

`run_status` 只能取下列值：

```text
budget_completed
early_completed_with_finding
early_completed_no_finding
unsupported
preprocessing_failed
online_start_failed
online_crashed
watchdog_timeout
runner_error
missing
```

`finding_found` 与 `run_status` 是独立字段。`preprocessing_failed`、`online_start_failed`、`online_crashed`、`watchdog_timeout`、`runner_error` 必须写入：

```json
{
  "failure_reason": {
    "code": "stable_machine_readable_code",
    "message": "human readable message",
    "log_tail_path": "relative/path"
  }
}
```

实验完成条件：

```text
raw-runs.jsonl line count == 1350
每个 run_key 恰好出现一次
support-matrix.csv row count == 270
missing run count == 0
unsupported run count == sum(unsupported tool-target rows in support-matrix.csv x 5)
validated input tree expected icsfuzz_full unsupported run count == 275
```

`experiment_status="complete"` 表示 1350 条权威记录完整、支持性矩阵完整、共同支持子集对比完整。它不表示 `supported tool-target pairs == 270`。声明三工具 90 目标全量实际 fuzzing 的实验必须使用新的 experiment ID，并把完成条件改为 `supported tool-target pairs == 270` 与 `unsupported runs == 0`。

## 7. Finding 与 oracle 标准

### 7.1 Common oracle

三工具横向主表只使用 common oracle。common oracle 包含：

```text
asan
ubsan
target_signal
target_oom
known_fault_replay
testcase_timeout
```

`testcase_timeout` 单独成列，不合并进 memory-safety finding。

### 7.2 SemantiST semantic oracle

SemantiST semantic objective 写入 SemantiST 专项表，并保留在 raw run record：

```text
semantic_objective_found
first_semantic_objective
semantic_objective_events
```

三工具主表中，`semantic_objective_found=true` 且 `common_oracle_finding_found=false` 的结果列为 `semantic_only`，不合并进 common finding 数。

### 7.3 Discovery phase

`discovery_phase` 只能取：

```text
initial_ingress
semantic_generation
fuzzing
tool_native_initialization
replay
```

`replay` 不改变首次发现时间。SemantiST `semantic_generation` 阶段发现计入 `semantist_full`。AFL++ calibration 阶段发现计入 `aflplusplus_full`。ICSFuzz 初始化输入触发 crash 计入 `icsfuzz_full`。

### 7.4 首次发现记录

每个 finding candidate 必须写入：

```json
{
  "tool": "semantist_full",
  "target_id": "{target_id}",
  "trial_id": 1,
  "discovery_phase": "semantic_generation",
  "oracle": "asan",
  "observed_monotonic_ns": 123456789000,
  "observed_elapsed_seconds": 0.842361,
  "tool_reported_time_seconds": null,
  "execution_ordinal": 6,
  "seed_sha256": "{sha256(seed_bytes)}",
  "artifact_path": "runs/{tool}/{suite}/{target_id}/trial_{trial_id:02d}/findings/{finding_id}"
}
```

没有 finding 的 trial 写入：

```json
{
  "finding_found": false,
  "censored": true,
  "censor_time_seconds": 300.0
}
```

### 7.5 去重

unique confirmed fault 的去重键按固定优先级生成：

```text
1. sanitizer_kind + normalized_source_location + normalized_stack_top
2. signal + normalized_source_location + normalized_stack_top
3. known_fault_id
4. semantist_stable_semantic_target_id + hazard_kind + source_location
5. oracle + target_id + normalized_reproducer_behavior
```

`seed_sha256` 不作为 unique fault 去重键。raw finding events 数量不作为漏洞数。

## 8. 覆盖率标准

### 8.1 禁用字段

`edge_coverage_new_edges_sum` 禁止用于正式结果。该字段在上一轮实验中恒为 0，不能代表最终覆盖。

### 8.2 Common coverage replay

覆盖率横向比较只使用 common coverage replay：

1. 每个 target 构建一个 semantic-off AFL coverage binary；
2. 收集每个 run 的最终 corpus；
3. 将工具 corpus 转换为共同 harness stdin 格式；
4. 对每个 testcase 执行 `afl-showmap`；
5. 对 AFL map slots 求并集；
6. 输出 slot 数、slot 集合 SHA-256、转换失败数量、crash testcase 覆盖贡献。

coverage replay binary 与 AFL++ preprocessing binary 使用同一构建方式：

```text
SEMANTIST_SEMANTIC_INSTRUMENTATION=off
SEMANTIST_AFL_COVERAGE_FLAGS=--fsanitize-coverage=trace-pc-guard
SEMANTIST_TARGET_SANITIZERS=--fsanitize=address,undefined -fno-omit-frame-pointer
```

工具内部 coverage 指标写入 `tool_specific_metrics`，不进入横向 coverage 主表。

### 8.3 SemantiST semantic coverage

SemantiST 专项表必须包含：

```text
stg_target_total
runtime_id_mapped_total
runtime_id_unmapped_total
semantic_hits_total
semantic_hit_target_ids_sha256
mapped_semantic_coverage_ratio
total_stg_coverage_ratio
state_signature_total
cycle_ids_observed_total
```

`mapped_semantic_coverage_ratio` 与 `total_stg_coverage_ratio` 的值域固定为 `[0, 1]`。

## 9. Adapter 合同

每个 adapter 必须实现相同接口：

```text
detect_support(target) -> support-result
prepare(target) -> preprocessing-result
materialize_trial(preprocessing-result, trial) -> trial-run-dir
run(trial-run-dir, budget, rng_seed, cpu) -> online-result
collect_corpus(trial-run-dir) -> testcase-list
collect_findings(trial-run-dir) -> finding-candidate-list
normalize_testcase(testcase) -> common-replay-input or unsupported-conversion
replay(testcase, replay-target) -> replay-result
collect_coverage(testcase-list, coverage-target) -> coverage-result
```

接口返回值必须 JSON serializable。adapter 不得通过自然语言日志作为唯一结果来源；日志只能作为辅助证据。缺失结构化文件时，adapter 必须写 `failure_reason.code="structured_result_missing"`。

## 10. 结果 schema

每个 row 生成一个 `run-result.json`。字段固定如下：

```json
{
  "schema_version": "semantist.three-tool-full-run/1.0.0",
  "experiment_id": "comparison_support_90targets_common_subset_300s_5trials",
  "run_key": "{run_key}",
  "tool": "semantist_full",
  "configuration_id": "full",
  "target_id": "{target_id}",
  "suite": "{suite}",
  "kind": "FUNCTION",
  "function": "{function}",
  "trial_id": 1,
  "rng_seed_requested": 1,
  "rng_seed_applied": 1,
  "rng_seed_status": "applied",
  "support_status": "supported",
  "preprocessing_status": "completed",
  "preprocessing_time_seconds": 0.0,
  "online_budget_seconds": 300,
  "t0_monotonic_ns": 123456000000,
  "observer_poll_interval_ms": 10,
  "online_elapsed_seconds": 300.0,
  "per_execution_timeout_ms": 1000,
  "execution_timeout_model": "per_process",
  "run_status": "budget_completed",
  "finding_found": false,
  "common_oracle_finding_found": false,
  "semantic_objective_found": false,
  "first_common_finding": null,
  "first_semantic_objective": null,
  "candidate_findings": [],
  "unique_confirmed_fault_ids": [],
  "total_executions": null,
  "execs_per_sec": null,
  "corpus_count": 0,
  "crash_count_raw": 0,
  "timeout_count_raw": 0,
  "common_replay": {
    "status": "not-run",
    "confirmed": false,
    "oracle": null
  },
  "common_coverage": {
    "status": "not-run",
    "covered_slots": null,
    "covered_slots_sha256": null,
    "conversion_failures": 0
  },
  "tool_specific_metrics": {},
  "artifact_paths": {},
  "failure_reason": null
}
```

缺失值使用 `null`。未采集数值不得写为 0。真实数量为 0 时写 0。

## 11. 输出布局

后续 agent 必须生成下列脚本与目录：

```text
/myfuzzer/SemantiST/experiments/comparison/
├── README.md
├── metrics_schema.json
├── run_full_comparison.py
├── adapters/
│   ├── semantist.py
│   ├── aflplusplus.py
│   └── icsfuzz.py
├── analysis/
│   ├── aggregate.py
│   ├── replay.py
│   ├── coverage.py
│   └── validate_complete.py
└── lib/
    ├── manifest.py
    ├── process.py
    ├── hashing.py
    └── schema.py
```

每次实验输出到：

```text
/myfuzzer/artifacts/experiments/{experiment_id}/
├── environment.json
├── experiment-manifest.json
├── manifest.snapshot.json
├── support-matrix.csv
├── schedule.jsonl
├── raw-runs.jsonl
├── raw-runs.csv
├── summaries/
│   ├── support.json
│   ├── findings_common_oracle.json
│   ├── findings_semantist_semantic.json
│   ├── coverage_common.json
│   ├── performance.json
│   └── stability.json
├── preprocessing/{tool}/{suite}/{target_id}/
├── common-replay/{suite}/{target_id}/
├── common-coverage/{suite}/{target_id}/
├── logs/{tool}/{suite}/{target_id}/trial_{trial_id:02d}/
└── runs/{tool}/{suite}/{target_id}/trial_{trial_id:02d}/
    ├── run-result.json
    ├── command.json
    ├── stdout.log
    ├── stderr.log
    ├── events/
    ├── corpus/
    ├── findings/
    └── tool-artifacts/
```

`raw-runs.jsonl` 由 1350 个 `run-result.json` 合并生成。summary 只能从 `raw-runs.jsonl`、replay results 和 coverage results 重建。

## 12. 回放与确认

每个 candidate finding 必须经过 common replay。common replay 使用 semantic-off sanitizer binary 与统一 stdin testcase 格式。replay 状态只能取：

```text
confirmed
unstable
not_reproduced
conversion_unsupported
replay_error
```

adapter 转换 testcase 时必须保存：

```text
original_testcase_path
normalized_testcase_path
normalization_metadata.json
```

转换无法保留字段值、字节顺序、cycle id 或 FUNCTION_BLOCK 多周期语义时，状态写 `conversion_unsupported`。adapter 不得猜测缺失字段。

## 13. 汇总表

最终 summary 必须生成下列表格。

### 13.1 支持矩阵

字段：

```text
tool
suite
target_id
kind
support_status
support_reason
preprocessing_status
preprocessing_time_seconds
```

### 13.2 Common oracle finding 主表

字段：

```text
tool
suite
target_count
supported_target_count
trial_count
supported_trial_count
common_oracle_finding_trials
common_oracle_finding_targets
unique_confirmed_faults
median_first_common_finding_time_seconds
median_first_common_finding_executions
censored_trials
```

### 13.3 SemantiST semantic 专项表

字段：

```text
suite
target_count
trial_count
semantic_objective_trials
semantic_objective_targets
initial_ingress_findings
semantic_generation_findings
fuzzing_findings
mapped_semantic_coverage_ratio_mean
total_stg_coverage_ratio_mean
state_signature_total
cycle_ids_observed_total
```

### 13.4 Coverage 与性能表

字段：

```text
tool
suite
target_id
trial_id
covered_slots
covered_slots_sha256
total_executions
execs_per_sec
corpus_count
online_elapsed_seconds
preprocessing_time_seconds
```

### 13.5 稳定性表

字段：

```text
tool
run_status
count
failure_reason_code
count_by_reason
```

## 14. 预检

正式全量运行前必须执行预检命令：

```bash
python3 experiments/comparison/run_full_comparison.py \
  --experiment-id comparison_support_90targets_common_subset_300s_5trials_preflight \
  --manifest /myfuzzer/SemantiST/benchmarks/external/manifest.json \
  --artifact-root /myfuzzer/artifacts/experiments \
  --cpus 1-2 \
  --preflight
```

预检目标固定为：

```text
oscat_basic_inc2
icsfuzz_bf_mcpy_1
scan_cycle_aircraft_oobw_4
oscat_basic_month_to_string
oscat_basic_fifo_16
```

预检的 `trial_id` 固定为 1。每个 selected target 与每个 tool 运行 1 个 20 秒 trial；unsupported target 仍生成 run-result。预检通过条件：

```text
preflight raw run count == selected_targets x 3 tools
每个 supported SemantiST row 写出 events.jsonl
每个 supported AFL++ row 写出 AFL fuzzer_stats 或 structured_result_missing
每个 supported ICSFuzz row 写出 wrapper.log 或 structured_result_missing
SemantiST semantic_generation 事件能被解析
SemantiST preprocessing/trial 目录均不存在 STG generated seeds before t0
ICSFuzz run-result.json 中 rng_seed_status == tool_missing_seed_control
observer_poll_interval_ms <= 10
首个 finding candidate 包含 observed_monotonic_ns 与 observed_elapsed_seconds
runner SIGTERM 不被记录为 target crash
validate_complete.py exit code == 0
```

预检失败时，正式实验不得启动。

## 15. 禁止事项

- 不删除 SemantiST semantic initial generator 的发现。
- 不把 SemantiST fuzz loop 前的发现记为 0 秒。
- 不使用 `python3 -m fuzzer.pipeline.core --build-only` 作为 SemantiST preprocessing。
- 不在 `t0_monotonic_ns` 前生成、摄入或复制 SemantiST STG generated seeds。
- 不从工具 UI 文本进入主循环的时刻开始计时。
- 不使用工具日志时间、文件修改时间或 Unix 时间作为首次 finding 主指标。
- 不给任一工具免费 target-specific online 初始化时间。
- 不给任一工具手工漏洞触发 seed。
- 不用 SemantiST generated seeds 作为 AFL++ 输入。
- 不用 SemantiST 或 AFL++ 结果替代 ICSFuzz。
- 不把 ICSFuzz 的 `rng_seed_status` 写成 `applied`。
- 不把 unsupported target 从矩阵中删除。
- 不把本实验称为“三工具 90 目标全量实际 fuzzing 对比”。
- 不把 raw finding metadata 数量当作 unique fault 数。
- 不把不同定义的工具内部 coverage 放在同一横向列。
- 不把 runner SIGTERM/SIGKILL 当作 target crash。
- 不把普通非零退出直接归类为漏洞。
- 不在 run matrix 执行中途修改工具算法后合并前后结果。
- 不只保留 summary 而删除 raw records、命令、日志、corpus、finding reproducer。
- 不用 `edge_coverage_new_edges_sum` 作为正式 coverage 指标。
- 不在 `common_all_90` 总数与 supported-only 子集之间混合比较发现总数。

## 16. 完成判定

完整实验完成必须同时满足：

```text
/myfuzzer/artifacts/experiments/comparison_support_90targets_common_subset_300s_5trials/raw-runs.jsonl exists
raw-runs.jsonl line count == 1350
support-matrix.csv row count == 270
missing run count == 0
unsupported run count == sum(unsupported tool-target rows in support-matrix.csv x 5)
validated input tree expected icsfuzz_full unsupported run count == 275
common_supported_targets is computed and saved in summaries/support.json
每个 run-result.json 通过 metrics_schema.json 校验
每个 supported run 有 command.json、stdout.log、stderr.log
每个 candidate finding 有 replay 状态
每个 coverage-supported run 有 common coverage 状态
summaries/ 下 5 个 summary JSON 全部存在
validate_complete.py exit code == 0
```

满足全部完成条件后，生成的脚本才能把实验状态写为：

```json
{
  "experiment_status": "complete"
}
```
