# 平台语义校准

后果判定要以真实 PLC 的行为为准。这组用例在 CODESYS 和我们的 fuzz 环境（RuSTy + OSCAT 兼容层 + 实验用的 sanitizer）上运行同一份 ST 代码，逐项对比两边的结果。用例覆盖：中间结果宽度、除零、REAL 转整数、TIME 的位宽与回绕、字符串函数越界、DATE/DT 范围，以及之前实验里出现过的 INC、INC2、REAL_TO_STRF、CHARNAME、CLEAN、FILTER_I、RMP_B。

目标配置：CODESYS V3.5 SP21 Patch 3，CODESYS Control Win V3 x64，oscat_basic 3.3.5.0。

## 文件

| 文件 | 内容 |
| --- | --- |
| `cases.csv` | 用例定义：编号、内容、是否可能触发异常、CODESYS 文档上的预期、关联的问题 |
| `record_sheet.csv` | 记录表。`rusty` 列已由脚本填好，`codesys` 列和 `note` 列待填 |
| `codesys/calib_cases.st` | 两边共用：`CALIB_INT`、`CALIB_REAL`、`CALIB_STR` 三个函数 |
| `codesys/calib_oscat.st` | 两边共用：`CALIB_OSCAT_STR`，需要 OSCAT 库 |
| `codesys/calib_prg.st` | 仅 CODESYS：`CALIB_PRG`，逐个运行用例并收集结果 |
| `codesys/calib_oscat_prg.st` | 仅 CODESYS：`CALIB_OSCAT_PRG`，OSCAT 相关用例，需要 OSCAT 库 |
| `run_rusty.py`、`rusty_driver.c` | 生成 `rusty` 列 |

## 在 CODESYS 上运行

### 1. 建工程

1. 新建标准工程，设备选 CODESYS Control Win V3 x64，语言选 ST。
2. 在库管理器里添加 oscat_basic 3.3.5.0。如果暂时不跑 OSCAT 部分，可以跳过这一步，同时跳过下面的 `CALIB_OSCAT_STR` 和 `CALIB_OSCAT_PRG`。
3. **不要**添加隐式检查 POU（CheckBounds、CheckDivInt、CheckRange 等），标准工程默认没有。如果工程里已经有了，请在环境信息里注明。

### 2. 添加 POU

按下表依次添加。每个 POU 的声明部分（从 `FUNCTION` 或 `PROGRAM` 开始，到最后一个 `END_VAR`）粘贴到声明编辑器，其余语句粘贴到实现编辑器。末尾的 `END_FUNCTION` 或 `END_PROGRAM` 不要粘贴。

| POU | 类型 | 来源 |
| --- | --- | --- |
| `CALIB_INT` | 函数，返回 LINT | `calib_cases.st` |
| `CALIB_REAL` | 函数，返回 LREAL | `calib_cases.st` |
| `CALIB_STR` | 函数，返回 STRING[80] | `calib_cases.st` |
| `CALIB_OSCAT_STR` | 函数，返回 STRING[80] | `calib_oscat.st` |
| `CALIB_PRG` | 程序 | `calib_prg.st` |
| `CALIB_OSCAT_PRG` | 程序 | `calib_oscat_prg.st` |

### 3. 挂到任务上

在 MainTask 里加入 `CALIB_PRG` 和 `CALIB_OSCAT_PRG`。任务周期保持 10 ms，记下看门狗设置。原有的 `PLC_PRG` 可以保留。

编译时出现隐式类型转换之类的警告可以忽略。如果报错，把错误信息发给我。

### 4. 运行

1. 登录、下载、启动。
2. 两个程序每个周期跑一个用例，几秒内就能跑完。跑完后 `done` 变为 TRUE。
3. **如果 PLC 因异常停机**：
   - 先记下设备日志里的异常信息，比如除零、访问违例；
   - 然后执行"复位（热）"，再启动。
   - 进度保存在 RETAIN 变量里，程序会把刚才那个用例记为 `编号=EXCEPTION`，然后接着跑下一个。
   - 可能触发异常的用例有 201–208、851–853、951、952、961、962。`970=EXCEPTION` 表示 H1–G2 这一步出了异常。
4. 想从头再跑一遍，在线把 `restart` 写成 TRUE。如果改过代码重新下载，进度也会清零。

### 5. 记录

1. 在监视窗口里查看 `CALIB_PRG.out` 和 `CALIB_OSCAT_PRG.out`。每一项的格式是 `编号=值`，H1、G1、T0 这类结果也在其中。
2. 记录方式二选一：
   - 填写 `record_sheet.csv` 的 `codesys` 列。异常停机的用例写 `EXCEPTION`，并在 `note` 里写上日志信息。
   - 或者直接把监视窗口截图发给我，我来填表。
3. 另外请记下这些环境信息：
   - CODESYS 版本、编译器版本、运行时版本；
   - 工程设置里"UTF-8 encoding for STRING"是否勾选；
   - 是否存在隐式检查 POU；
   - 任务周期和看门狗设置。

## 重新生成 rusty 列

在仓库根目录、用实验镜像运行：

```bash
podman run --rm -v "$PWD:/repo:Z" -w /repo -e PYTHONPATH=/repo IMAGE \
    python3 experiments/calibration/run_rusty.py
```

脚本用 OSCAT 的兼容层和实验时的 sanitizer 编译这些用例，每个用例在单独的进程里运行，然后改写 `record_sheet.csv`。注意：它会清空已经填写的 `codesys` 列，填写前请先备份。编译用的是 `-O0`，以免常量折叠掩盖运行时行为（比如除零陷阱）。
