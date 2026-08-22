# SemantiST STG 语义任务排序算法

## 1. 文档目的

本文档定义 SemantiST 默认语义模式下的任务排序、Seed 选择、GuideTarget
推进、失败衰减和任务状态迁移算法。

算法的静态输入 Schema 为：

```text
semantist.semantic-task-plan/1.0.0
```

运行时快照 Schema 为：

```text
semantist.semantic-task-state/1.0.0
```

排序算法不使用源码条件字符串、正则分支目标、LLM 风险分数或人工漏洞类型
分数。默认语义模式只使用 STG Stable Target ID 和运行时 Observer 证据。

## 2. 核心原则

排序算法遵循以下原则：

1. `SemanticTaskPlan` 保存静态语义事实。
2. `SemanticRuntimeMetadata` 保存动态覆盖、优先级和尝试历史。
3. Seed 是具体输入序列，不是路径证明。
4. 目标覆盖只能由语义 Observer 返回的 Stable Target ID 确认。
5. GuideTarget 是当前 Seed 沿活动路径遇到的第一个未覆盖目标。
6. 任务收益来自可达语义目标，不来自漏洞类型经验打分。
7. 纯 Fuzzer 只允许延后任务，不声明任务不可满足。

静态计划和动态状态的边界如下：

| 信息 | 所属实体 |
| --- | --- |
| 候选路径、控制义务、数据依赖、状态 Transfer、时间要求 | SemanticTaskPlan |
| Stable Target ID 与 Runtime ID 映射 | STG Runtime ID 表 |
| Seed 覆盖、周期、状态签名、目标亲和度 | SeedProfile |
| 当前任务、GuideTarget、优先级、失败次数 | SemanticRuntimeMetadata |
| 实际覆盖事实 | Semantic Observer |

## 3. 排序输入

对 POU `P`，排序输入定义为：

```text
R_P = <TaskPlan_P, SeedProfiles, RuntimeState, Coverage>
```

其中：

- `TaskPlan_P` 是 POU 的语义任务集合。
- `SeedProfiles` 是可选 Corpus 输入的运行时画像。
- `RuntimeState` 是任务状态和尝试历史。
- `Coverage` 是 Observer 已确认的全局 Stable Target ID 集合。

每个 `SemanticTask` 至少包含：

```text
task_id
task_kind
pou
terminal_target_id
candidate_paths
ordered_waypoints
control_obligations
data_dependencies
state_transfer_chains
temporal_requirements
hazard_prerequisites
controllable_seed_fields
modeling_status
unlocked_targets
```

## 4. SeedProfile

每个初始 Seed 和新 Corpus 输入在普通执行后生成 `SeedProfile`：

```text
SeedProfile = <
  content_hash,
  covered_target_ids,
  covered_targets_by_cycle,
  cycle_ids,
  state_signatures,
  target_affinity,
  parent_id
>
```

字段含义如下：

| 字段 | 含义 |
| --- | --- |
| content_hash | Seed 内容 SHA-256，也是稳定 Seed 标识 |
| covered_target_ids | Observer 确认的 Stable Target ID 集合 |
| covered_targets_by_cycle | 按扫描周期保存的语义覆盖 |
| cycle_ids | Seed 实际执行的周期编号 |
| state_signatures | Function Block 状态签名 |
| target_affinity | Seed 对各任务已有路径进度的归一化估计 |
| parent_id | 生成该 Seed 的父输入 |

`target_affinity(s, task)` 当前按 Seed 已覆盖的任务 Waypoint 比例计算：

```text
Affinity(s, Task) =
  |Cov(s) intersect Waypoints(Task)| / |Waypoints(Task)|
```

它只用于估计剩余依赖和状态工作量，不能替代覆盖证据。

输入支持：

```text
NAME,TYPE,VALUE
cycle.NAME,TYPE,VALUE
```

即使 Seed 文本中出现某个目标名称，也不能据此把目标加入
`covered_target_ids`。

## 5. 候选路径与 GuideTarget

任务的一条候选路径表示为：

```text
pi = [t1, t2, ..., tn]
```

其中每个 `ti` 都是 STG Stable Target ID。

对 Seed `s`，GuideTarget 定义为：

```text
Guide(s, pi) = first ti in pi where ti not in Cov(s)
```

若路径全部覆盖，则：

```text
Guide(s, pi) = None
```

### 5.1 活动路径选择

对多条候选路径，当前实现按以下稳定次序选择：

1. 剩余未覆盖 Waypoint 数量最少。
2. 完整路径长度最短。
3. Stable Target ID 序列字典序最小。

定义：

```text
Remaining(pi, s) =
  suffix of pi beginning at Guide(s, pi)
```

则：

```text
ActivePath(s, Task) =
  argmin_pi (
    |Remaining(pi, s)|,
    |pi|,
    lexicographic(pi)
  )
```

路径由有限 SCC 展开生成。排序器不会为了估算工作量无限展开循环。

### 5.2 自动推进

Observer 覆盖当前 GuideTarget 后：

1. Stable Target ID 加入全局覆盖集合。
2. `coverage_epoch` 增加。
3. 在当前活动路径上重新计算第一个未覆盖目标。
4. 若终点已覆盖，任务进入 `Covered`。
5. 否则新的第一个未覆盖目标成为 GuideTarget。

## 6. 剩余语义工作量

对 Seed `s` 和任务 `Task`：

```text
Effort(s, Task) = EC + ED + ES + ET + EH
```

五个维度都使用当前 POU 的静态最大值归一化：

```text
NX(P) = max(1, max raw_X(Task in P))
Normalized_X = raw_X / NX(P)
```

计划生成器把 `NEC`、`NED`、`NES`、`NET` 和 `NEH` 保存到：

```text
normalization_by_pou
```

### 6.1 控制工作量 EC

`EC` 是活动路径剩余部分中的控制 Outcome 数量：

```text
raw_EC =
  |{o in ControlObligations(Task) |
      o.target_id in Remaining(ActivePath, s)}|

EC = raw_EC / NEC(P)
```

控制义务来自 Evaluation Edge 和 Typed Expression DAG。

### 6.2 数据依赖工作量 ED

`ED` 表示尚需通过可控输入满足的 Def-Use 和输入依赖：

```text
raw_ED =
  |DataDependencies(Task)| * (1 - Affinity(s, Task))

ED = raw_ED / NED(P)
```

运行时变异器只修改 `controllable_seed_fields` 所描述的真实可控字段。

### 6.3 状态迁移工作量 ES

`ES` 表示尚需完成的状态 Transfer：

```text
StateProgress(s, Task) =
  Affinity(s, Task), if state_signatures(s) is not empty
  0,                 otherwise

raw_ES =
  |StateTransferChains(Task)| * (1 - StateProgress(s, Task))

ES = raw_ES / NES(P)
```

状态签名只能说明 Seed 产生了可区分状态，不能直接证明某条 Transfer 已完成。

### 6.4 时间工作量 ET

对每个时间要求 `r`：

```text
required_steps(r) = max(0, r.minimum_cycles - 1)
```

任务所需时间步和 Seed 已完成时间步为：

```text
RequiredTemporal =
  sum required_steps(r)

CompletedTemporal =
  max(0, |cycle_ids(s)| - 1)
```

因此：

```text
raw_ET = max(0, RequiredTemporal - CompletedTemporal)
ET = raw_ET / NET(P)
```

该维度用于区分单周期输入和已具备多周期状态推进的输入。

### 6.5 危险阶段工作量 EH

`EH` 表示尚未覆盖的危险前置目标和终点：

```text
raw_EH =
  |{h in HazardPrerequisites(Task) | h not in Cov(s)}|
  + indicator(TerminalTarget(Task) not in Cov(s))

EH = raw_EH / NEH(P)
```

对 HazardViolation，`HazardReach` 必须先作为前置 Waypoint。覆盖
`HazardReach` 只减少工作量，不构成漏洞。

## 7. Seed 选择

对每个可调度任务，计算所有 Seed 的 Effort：

```text
BestSeed(Task) =
  argmin_s (
    Effort(s, Task),
    content_hash(s)
  )
```

第二项是确定性并列规则。

若 Corpus 暂时没有 Seed，则使用空 `SeedProfile` 计算默认工作量，任务仍可参与
排序，但 `best_seed_id` 为 `null`。

## 8. 任务收益

任务收益定义为：

```text
Gain(Task) =
  ExactViolationGain
  + UnlockedTargetGain
  + SemanticNovelty
```

### 8.1 Exact Violation 收益

```text
ExactViolationGain = 1
```

仅当同时满足：

- `task_kind == violation`
- `modeling_status == exact`
- `terminal_target_id` 尚未覆盖

否则为 `0`。

Conservative 和 Opaque 不会被猜测升级为 Exact。

### 8.2 解锁目标收益

```text
UnlockedTargetGain =
  |{t in unlocked_targets(Task) | t not in GlobalCoverage}|
```

它表示完成该任务路径可能带来的额外语义覆盖和危险目标解锁。

### 8.3 语义新颖性

当前引导目标共有七类：

```text
BranchOutcome
CaseOutcome
LoopOutcome
ControlTransfer
Cycle
HazardReach
terminal semantic target
```

任务控制义务中的语义种类越丰富，新颖性越高：

```text
SemanticNovelty =
  min(1, |distinct control obligation kinds| / 7)
```

## 9. 静态优先级与失败衰减

任务基础优先级为：

```text
Priority(Task) =
  Gain(Task) / (1 + Effort(BestSeed(Task), Task))
```

运行时动态优先级为：

```text
DynamicPriority(Task) =
  Priority(Task) * exp(-rho * failed_attempts)
```

当前参数：

```text
rho = 0.25
```

失败次数只保存在运行时 State，不回写 STG 或静态任务计划。

完整任务排序按以下顺序：

1. `DynamicPriority` 降序。
2. `task_id` 字典序升序。

非活动状态任务仍保留在完整 ranking 尾部，状态次序为：

```text
Ready/Active
Deferred
Blocked
Unmapped
Covered
```

## 10. 任务状态机

任务状态包括：

| 状态 | 含义 |
| --- | --- |
| Blocked | 任务没有可执行候选路径 |
| Ready | 可以参与排序 |
| Active | 当前选中的任务 |
| Covered | 终点 Stable Target ID 已覆盖 |
| Deferred | 多次预算失败后暂时延后 |
| Unmapped | 终点没有可执行语义探针映射 |

状态迁移如下：

```text
Ready -> Active
Active -> Covered
Active -> Deferred
Deferred -> Ready
Active -> Ready
```

规则如下：

- 任务被选中时进入 `Active`。
- 切换到另一任务时，旧 `Active` 返回 `Ready`。
- 当前终点被 Observer 覆盖时进入 `Covered`。
- 连续 3 次预算失败后进入 `Deferred`。
- `Deferred` 记录当时的 `coverage_epoch`。
- 任意新的语义覆盖使 `coverage_epoch` 增加，旧 `Deferred` 重新进入 `Ready`。
- 没有 IR 探针映射的任务保持 `Unmapped`。
- 纯 Fuzzer 不产生 `Infeasible` 状态。

## 11. 事件驱动调度流程

初始化或 `ranking_dirty` 事件发生时，语义调度执行以下步骤：

```text
1. 刷新所有任务状态
2. 过滤 Ready 和 Active 任务
3. 为每个任务选择最小 Effort Seed
4. 选择该 Seed 的活动候选路径
5. 计算 GuideTarget
6. 计算 Gain、Priority 和 DynamicPriority
7. 生成完整 ranking
8. 选择最高优先级任务
9. 把任务、Seed、路径和 GuideTarget 安装到运行时上下文
```

结果写入缓存的 `ActiveSelection`。在当前 `semantic-task-budget` 内且没有语义
失效事件时，普通 `Scheduler::next()` 直接返回缓存的 Corpus ID 和活动选择，
不再扫描 Corpus，也不再执行 Task×Seed 排序。

State 维护：

```text
content_hash -> CorpusId
content_hash -> SeedProfile
Task -> Top-K minimum-effort seeds
ActiveSelection
ranking_dirty
coverage_epoch
corpus_epoch
ranking/cache/rerank-reason statistics
```

Seed 加入 Corpus 时只构造一次 `SeedProfile`。新 Seed 只与语义覆盖、状态签名、
时间步或 target affinity 受影响的 Task 比较并增量更新 Top-K。只新增 AFL edge、
没有新增语义目标或状态信息的 Seed 会进入缓存，但不会触发全量任务重排。

允许设置 `ranking_dirty` 的事件为：

1. 初始化和首批 SeedProfile 建立；
2. GuideTarget 或终点 Stable Target ID 被 Observer 覆盖；
3. 新 Seed 产生新语义目标、状态签名或相关多周期进度；
4. Task 进入 Deferred 或在新 `coverage_epoch` 后重新激活；
5. 当前 BestSeed 被移除或失效。

选择结果包含：

```text
task_id
terminal_target_id
guide_target_id
active_path
remaining_obligations
best_seed_id
priority
control_obligation
controllable_seed_fields
backward_dependencies
hazard_prerequisites
modeling_status
goal_expression
cycle_hint
```

Mutator、预算 Stage 和外部符号接口都消费同一个 GuideTarget Stable ID。

## 12. 运行时 JSON

`RUN_DIR/semantic-task-state.json` 是原子写入的紧凑运行时快照：

```json
{
  "schema_version": "semantist.semantic-task-state/1.0.0",
  "coverage_epoch": 8,
  "corpus_epoch": 23,
  "active_task_id": "semantic-task:...",
  "guide_target_id": "target:...",
  "active_path": ["target:...", "target:..."],
  "best_seed_id": "sha256...",
  "ranking": ["semantic-task:...", "semantic-task:..."],
  "ranking_total": 42,
  "ranking_truncated": true,
  "ranking_dirty": false,
  "ranking_statistics": {
    "ranking_count": 9,
    "cache_hit_count": 12000,
    "full_rebuild_count": 9,
    "incremental_seed_updates": 23,
    "seed_profile_build_count": 23,
    "corpus_scan_count": 0,
    "rerank_reasons": {"semantic_coverage": 4}
  },
  "covered_target_count": 17,
  "active_task_state": {
    "status": "active",
    "failed_attempts": 1,
    "attempts": 4,
    "priority": 0.73,
    "base_priority": 0.94,
    "gain": 2.5,
    "minimum_effort": 1.66,
    "best_seed_id": "sha256...",
    "active_path": ["target:...", "target:..."],
    "guide_target_id": "target:...",
    "remaining_obligations": ["target:..."],
    "prerequisites": ["target:..."],
    "unlocked_targets": ["target:..."]
  },
  "attempt_history_len": 4
}
```

默认快照只保留主线调度和报告所需摘要：当前任务、GuideTarget、活动路径、最佳
Seed、ranking 前缀、ranking 总数、覆盖计数、活动任务状态和尝试历史长度。
设置 `SEMANTIST_SEMANTIC_STATE_DETAIL=1` 时才额外写出完整
`covered_target_ids`、`task_states` 和 `attempt_history`。

该文件用于观察、报告和恢复诊断，不是 STG 静态事实来源。

## 13. 与 LibAFL 实体的关系

| 实体 | 排序职责 |
| --- | --- |
| State | 保存计划、SeedProfile 缓存、Top-K、活动选择、epoch、ranking 和尝试历史 |
| Scheduler | 增量更新 Top-K；事件发生时重排；普通 next() 返回缓存 Seed |
| Mutator | 消费 GuideTarget、Goal Expression、类型和依赖 |
| Observer | 返回实际覆盖的 Stable Target ID 和周期 |
| Feedback | 接纳新语义覆盖、新状态签名、新 AFL edge 或解锁任务的输入 |
| Objective | 只接受 Exact Violation、Crash、Timeout 和 OOM |
| Stage | 管理任务预算、失败计数、优先级衰减和 Deferred |

所有输入都必须像普通 Seed 一样执行。只有 Observer 命中对应
`guide_target_id`，本次尝试才记为覆盖成功。

## 14. 轻量定向变异与预算降权

GuideTarget 在固定 budget 内长期无进展时，Stage 记录一次失败尝试并触发
priority decay。连续失败的 Task 会进入 Deferred，让调度器把 fuzzing 时间转给
其他 Ready Task；新的 `coverage_epoch` 会重新激活 Deferred Task。

Mutator 消费当前 `ActiveSelection` 中的 Goal Expression、控制义务、反向依赖、
可控 Seed 字段、Transfer Summary、时间要求、建模状态以及 IEC 类型事实。
它只做轻量定向：

1. 比较表达式 `=`, `<>`, `<`, `<=`, `>`, `>=` 推向相邻边界值；
2. `AND` 范围表达式保持为一组可满足的 `low < x < high`；
3. division/modulo hazard 定向修改分母；
4. 整数、字符串、数组和指针使用 IEC 边界字典；
5. `REAL`/`LREAL` arithmetic-boundary hazard 使用有限 IEEE 边界值，并同步维护
   min/max guard。

复杂路径条件、不可达 conservative violation 和全程序约束不由独立约束模块处理，
而由普通 fuzzing 次数、调度探索和后续语义提取过滤共同承担。

## 15. 确定性与可复现性

排序结果通过以下规则保持确定性：

- Stable Target ID 不重新编号。
- `task_id` 由静态语义身份稳定生成。
- Seed 并列时按 `content_hash` 排序。
- 路径并列时按 Stable Target ID 序列排序。
- 任务并列时按 `task_id` 排序。
- 动态衰减只依赖持久化的 `failed_attempts`。

相同计划、SeedProfile、覆盖集合和尝试历史应产生相同 ranking。

## 16. 复杂度与当前边界

缓存建立或全局语义覆盖变化时允许 O(Task×TopK) 重排。新 Seed 的增量工作量
与受影响 Task 数和固定 Top-K 有关。无失效事件的 `Scheduler::next()` 为 O(1)
哈希索引和缓存读取，不随完整 Corpus 或 Task×Seed 乘积增长。

当前算法保留以下边界：

- `target_affinity` 是进度估计，不是 Def-Use 完成证明。
- 状态签名表示状态新颖性，不直接证明具体 Transfer。
- 候选路径先按剩余 Waypoint 数选择，再计算五维任务 Effort。
- SemanticNovelty 使用控制义务种类比例，不是全局稀有度模型。
- `Blocked` 表示缺少可执行路径，不表示语义上不可达。
- `Unmapped` 表示缺少探针映射，不表示目标不存在。
- Conservative 和 Opaque 目标可用于引导或探索，但不是 Exact Objective。
- 轻量定向变异不提供不可满足性证明，预算耗尽只表示当前 fuzzing 窗口内没有进展。
- Conservative 和 Opaque 不会因变异命中路径而升级为 Exact Objective。

这些边界必须在后续扩展中显式改进，不能通过人工风险分数或源码字符串推断
绕过。
