# RuSTy 编译器漏洞报告：OSCAT `LEAP_OF_DATE` 中 DWORD 高位比较被错误编译

## 建议标题

RuSTy miscompiles OSCAT `LEAP_OF_DATE`: `DWORD` high-bit comparison against `16#80000000` becomes false because the left side is sign-extended while the literal is emitted as positive `i64`

## 摘要

在 RuSTy `be1de6f175ec1bed7928904dfb694208a24043fb` 上，官方 OSCAT Basic 库中的 `LEAP_OF_DATE` 会被静默误编译。源码本意是比较 32 位位模式 `0x80000000`：

```iecst
LEAP_OF_DATE := SHL(((DATE_TO_DWORD(idate) + 43200) / 31557600), 30) = 16#80000000;
```

但 RuSTy 生成的 LLVM IR 会把左侧 `SHL` 得到的 `i32 0x80000000` 用 `sext` 符号扩展成负的 `i64 -2147483648`；同时右侧 `16#80000000` 被解析成普通整数值 `2147483648`，并直接生成为正的 `i64 2147483648`。最终实际比较：

```text
-2147483648 == 2147483648
```

因此本应为 `TRUE` 的闰年判断返回 `FALSE`。这会影响依赖 `LEAP_OF_DATE` 的 `DAYS_IN_MONTH`，例如闰年 2 月可能被按非闰年路径处理。

## 受测版本

本报告确认问题存在于以下 RuSTy 提交：

```text
commit be1de6f175ec1bed7928904dfb694208a24043fb
AuthorDate: Mon May 11 12:32:29 2026 +0200
CommitDate: Mon May 11 10:32:29 2026 +0000
Subject: fix(lowering): disambiguate per-unit ctor symbols by path hash (#1724)
```

复现时从同一提交通过 `git archive HEAD` 导出干净源码到 `/tmp/rusty-be1de6f175-clean`，再执行 `cargo build --release --bin plc`。本地工作区中的 RuSTy checkout 有 SemantiST 语义元数据补丁，因此本报告的核心证据来自 `/tmp` 中的干净 RuSTy 源码导出。

## OSCAT 触发点

OSCAT Basic 的 `DAYS_IN_MONTH` 依赖 `LEAP_OF_DATE(IDATE)` 判断闰年分支：

```iecst
DAYS_IN_MONTH := DAY_OF_YEAR(IDATE);
IF LEAP_OF_DATE(IDATE) THEN
    (* leap-year month handling, February => 29 *)
ELSE
    (* non-leap-year month handling, February => 28 *)
END_IF;
```

`LEAP_OF_DATE` 的源码如下：

```iecst
FUNCTION LEAP_OF_DATE : BOOL
VAR_INPUT
    idate : DATE;
END_VAR

LEAP_OF_DATE := SHL(((DATE_TO_DWORD(idate) + 43200) / 31557600), 30) = 16#80000000;
END_FUNCTION
```

这里 `DATE_TO_DWORD(idate)` 返回日期相对 1970-01-01 的秒数。OSCAT 公式通过近似年序号的低位模式判断闰年：当中间结果满足特定位模式时，`SHL(..., 30)` 应产生 `DWORD` 位模式 `0x80000000`，与 `16#80000000` 比较后返回 `TRUE`。

## 错误 IR

使用干净 RuSTy 编译 OSCAT 后，`LEAP_OF_DATE` 的关键 LLVM IR 如下：

```llvm
%call = call i32 @DATE_TO_DWORD(i64 %load_idate)
%tmpVar = add i32 %call, 43200
%tmpVar1 = sdiv i32 %tmpVar, 31557600
%1 = shl i32 %tmpVar1, 30
%2 = sext i32 %1 to i64
%tmpVar2 = icmp eq i64 %2, 2147483648
```

问题集中在最后两句：

```llvm
%2 = sext i32 %1 to i64
%tmpVar2 = icmp eq i64 %2, 2147483648
```

如果闰年输入使 `%tmpVar1 = 2`，则：

```text
%1 = shl i32 2, 30
   = i32 0x80000000
```

随后 `sext i32 %1 to i64` 是符号扩展。由于 `0x80000000` 的最高位是 1，`sext` 会把它当成 signed `i32` 负数：

```text
i32 0x80000000
sext to i64
= i64 0xFFFFFFFF80000000
= -2147483648
```

右侧 IR 常量则已经是 `i64 2147483648`：

```text
i64 2147483648
= 0x0000000080000000
```

所以最终比较不是两个相同的 32 位 `0x80000000`，而是：

```text
left  = 0xFFFFFFFF80000000  (-2147483648)
right = 0x0000000080000000  ( 2147483648)
```

结果必然为 `FALSE`。

## 右侧为什么直接是 `i64 2147483648`

IR 中没有右侧的 `zext`，因为右侧并不是运行时从 `i32 0x80000000` 零扩展而来。RuSTy 在生成 IR 前已经把 `16#80000000` 当成普通整数 literal 处理，其数值是 `2147483648`。由于该值超过 signed `DINT/i32` 最大值 `2147483647`，RuSTy 默认将它归类为 `LINT/i64`，然后直接生成 `i64 2147483648` 常量。

RuSTy 源码中相关逻辑如下：

```rust
AstLiteral::Integer(value) => {
    self.annotate(statement, StatementAnnotation::value(get_int_type_name_for(*value)));
}
```

```rust
fn get_int_type_name_for(value: i128) -> &'static str {
    if i32::MIN as i128 <= value && i32::MAX as i128 >= value {
        DINT_TYPE
    } else {
        LINT_TYPE
    }
}
```

因此：

```text
16#80000000 -> integer value 2147483648
2147483648 > i32::MAX
=> literal type = LINT
=> LLVM type = i64
```

生成数字常量时，RuSTy 又会根据该 literal 的类型直接创建目标 LLVM 类型的常量：

```rust
let literal_type = self.llvm_index.get_associated_type(literal_type_name)?;
self.llvm.create_const_numeric(&literal_type, number, stmt.get_location())
```

```rust
BasicTypeEnum::IntType { 0: int_type } => int_type
    .const_int_from_string(value, StringRadix::Decimal)
```

所以右侧不是：

```text
i32 0x80000000 -> zext -> i64 0x0000000080000000
```

而是：

```text
integer literal value 2147483648 -> directly create i64 2147483648
```

这正是两边不一致的根因之一：左侧是从 `i32` 结果经 `sext` 提升，右侧则直接以正的 `i64` 常量出现。

## 实际错误计算

以 `1972-01-01` 为例，从 1970-01-01 到 1972-01-01 是 730 天：

```text
DATE_TO_DWORD(D#1972-01-01) = 730 * 86400 = 63072000
tmp = 63072000 + 43200 = 63115200
q = tmp / 31557600 = 2
SHL(q, 30) = 2 << 30 = 0x80000000
```

按 OSCAT 源码语义：

```text
0x80000000 = 16#80000000
=> TRUE
```

按 RuSTy 当前 IR 实际执行：

```text
left  = sext(i32 0x80000000) = i64 0xFFFFFFFF80000000 = -2147483648
right = i64 2147483648       = i64 0x0000000080000000
left == right                = FALSE
```

先前运行期验证显示，直接调用编译产物中的 `LEAP_OF_DATE` 时，`1972-01-01` 对应输入返回 `0/FALSE`：

```text
day_730_ns idate=63072000000000000 result=0
```

该输入期望结果应为 `TRUE`。

## 为什么这是语义错误

`DWORD` 在 IEC 61131-3/ST 中表示 32 位双字/位串类型。`16#80000000` 在该表达式中用于表达 32 位最高位为 1 的位模式。源码层面比较的是位模式相等性，而不是把左侧当 signed `DINT` 负数、右侧当 positive `LINT` 正数进行数值比较。

这个问题不是 LLVM 的 `sext` 指令本身错误，而是 RuSTy 在类型推导和 lowering 时选择了不一致的语义：

- 左侧 `DATE_TO_DWORD(idate)` 返回 `DWORD/i32`，但与无类型十进制字面量 `43200`、`31557600` 混合运算后，IR 中出现 `sdiv i32`，说明该算术链被按 signed `i32` 处理。
- 左侧 `SHL` 结果为 `i32 0x80000000` 后，被 `sext` 到 `i64`，成为 `-2147483648`。
- 右侧 `16#80000000` 被当作普通整数值 `2147483648`，由于超过 `DINT` 范围，被直接生成为 `LINT/i64 2147483648`。

如果编译器选择 64 位比较，则左侧应保留 `DWORD`/无符号/位串语义，例如使用 `zext i32 %1 to i64`。如果编译器选择 32 位比较，则两侧应保持同宽位模式比较。当前 IR 把同一个源码位模式拆成一负一正，导致判断错误。

## 影响

已确认影响：

- 官方 OSCAT Basic 的 `LEAP_OF_DATE` 被错误编译。
- 依赖 `LEAP_OF_DATE` 的 `DAYS_IN_MONTH` 闰年路径可能不可达。
- RuSTy 编译产物会把某些本应为 `TRUE` 的 `DWORD` 高位位模式比较算成 `FALSE`。

潜在影响：

- 使用同类 `DWORD`/`UDINT`/位串表达式的 PLC 程序可能发生静默控制流偏移。
- 如果类似表达式参与限位、联锁、状态字或安全条件判断，可能造成安全相关逻辑判断错误。

本报告没有给出 CVSS 分数，也不声称已验证远程攻击路径。当前证据证明的是确定性的编译器语义误编译。

## 复现步骤

1. 使用受测 RuSTy 提交构建编译器：

```bash
cargo build --release --bin plc
```

2. 编译包含 OSCAT Basic `oscat.st` 的工程，生成单模块 LLVM IR：

```bash
plc build plc.json \
  --ir \
  --single-module \
  --error-format none \
  --build-location /tmp/rusty-oscat-build \
  -o /tmp/rusty-oscat.ll
```

3. 在生成的 IR 中搜索 `@LEAP_OF_DATE`，检查是否出现：

```llvm
%tmpVar1 = sdiv i32 %tmpVar, 31557600
%1 = shl i32 %tmpVar1, 30
%2 = sext i32 %1 to i64
%tmpVar2 = icmp eq i64 %2, 2147483648
```

4. 对 `D#1972-01-01` 或等价 DATE 运行 `LEAP_OF_DATE`。期望结果为 `TRUE`，实际编译产物返回 `FALSE`。

## 修复建议

建议维护者重点检查：

- 十六进制 literal `16#80000000` 在 `DWORD`/位串比较上下文中的类型选择。
- `DWORD` 与无类型十进制整数字面量混合二元运算时的共同类型推导。
- `SHL` 对 `DWORD`/位串输入的返回类型传播。
- 比较运算两侧类型统一时，`DWORD`/`UDINT`/位串值扩展到更宽整数的 signedness 选择。

建议增加 OSCAT 级别回归测试：

```text
LEAP_OF_DATE(D#1972-01-01) = TRUE
DAYS_IN_MONTH(D#1972-02-01) = 29
```

如果 RuSTy 的语言设计认为 `DWORD + integer literal` 不应被接受，那么编译器也应在类型检查阶段报错，而不是静默生成会改变逻辑结果的代码。

## 证据文件

- 干净 RuSTy 源码导出：`/tmp/rusty-be1de6f175-clean`
- OSCAT 复现 IR：`/tmp/rusty-be1de6f175-clean-leap.ll`
- SemantiST 实验证据目录：`artifacts/benchmarks/compiler_semantics_leap_of_date_20260714_073516/`
- OSCAT 源码：`benchmarks/oscat_basic/source/oscat.st`

## 结论

这是一个可在干净 RuSTy 提交上稳定复现的静默误编译问题。核心错误不是单纯的 `sext` 指令存在，而是左右两侧的类型和常量生成语义不一致：左侧 32 位 `0x80000000` 被符号扩展成负的 `i64`，右侧 `16#80000000` 被解析为普通整数值并直接生成为正的 `i64 2147483648`。因此源码中本应相等的位模式在 IR 中变成了一负一正，最终把闰年判断编译为 `FALSE`。
