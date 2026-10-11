# 银行流水导入与对账方案

- 状态：Design / Approved direction
- 范围：工商银行、邮储银行、招商银行、建设银行流水导入；与已导入微信 / 支付宝 / 银行交易的对账、确认与写入
- 相关文档：
  - `docs/design/import-financial-events.md`
  - `docs/SYNC_ARCHITECTURE.md`

---

## 1. 目标

银行流水不能作为普通账单直接导入。一个真实经济动作可能同时出现在银行流水和微信 / 支付宝账单中；如果两边都创建支出或收入，会重复记账。

目标流程：

```text
银行 PDF / XLS
  ↓ 解析为标准银行流水
  ↓ 与目标账本中已有交易对账
  ↓ 生成 matched / suspected / unmatched / transfer 预览
  ↓ 用户确认
  ↓ 只写入确认后的结果
```

核心目标：

1. 不重复创建微信 / 支付宝账单中已经存在的交易。
2. 花呗、信用卡还款固定建模为 `transfer`，不作为支出或收入。
3. 平台充值、提现、余额宝转入转出固定建模为 `transfer`。
4. 银行独有的工资、利息、手续费、ATM 存取款、银行直付消费才创建新交易。
5. 用户确认前不写账本。
6. 测试使用新账本和 Mock 支付宝 / 微信数据，不修改真实默认账本。

---

## 2. 当前约束与前提

### 2.1 银行 PDF 可能已脱敏

当前测试 PDF 中的金额、余额等信息已脱敏。因此：

- 余额链不平不代表解析失败；
- 不能用脱敏 PDF 直接做业务语义对账；
- 脱敏 PDF 只用于验证 parser 的结构抽取能力；
- 对账语义测试必须使用 Mock 支付宝 / 微信数据和新账本。

余额链异常应作为 `balance_chain_warning` 保留，不自动修正金额或余额。

### 2.2 对账对象是账本内数据

银行流水不与原始微信 / 支付宝文件直接对账，而是与目标账本中已经导入的交易对账：

```text
target ledger snapshot
  → items[] / transactions
  → 对账
```

这样可以覆盖：

- 微信账单已导入；
- 支付宝账单已导入；
- 用户手工修改过分类、备注、账户；
- 用户手工补录过交易；
- 多份账单重复导入后由账本层统一去重。

### 2.3 固定时间窗口

普通匹配窗口固定为：

```text
±3 自然日
```

不提供用户配置项。原因是用户通常不知道正确的结算延迟窗口，配置项只会增加误配风险。

花呗 / 信用卡还款不适用这个窗口去匹配原始消费。还款时间和消费时间可能相差很多天，应直接识别为还款 transfer。

---

## 3. 数据模型

### 3.1 银行流水标准行

所有银行 PDF / XLS 先解析为统一结构：

```ts
BankRow {
  provider: 'icbc' | 'psbc' | 'cmb' | 'ccb';
  accountRef: string;
  happenedAt: string;         // 本地墙钟时间，不带时区
  amount: Decimal;            // 保留银行原始正负号
  balanceAfter: Decimal;
  currency: 'CNY';
  summary: string;
  counterparty: string;
  counterpartyAccount?: string;
  channel?: string;
  externalRef?: string;
  rawHash: string;
  page: number;
  row: number;
  warnings: string[];
}
```

`amount` 保留银行原始方向：

| 金额方向 | 含义 |
|---|---|
| `-` | 银行账户资金流出 |
| `+` | 银行账户资金流入 |

`rawHash` 用于幂等和重复识别：

```text
sha256(
  provider,
  accountRef,
  happenedAt,
  amount,
  balanceAfter,
  currency,
  summary,
  counterparty,
  counterpartyAccount,
  channel,
  externalRef
)
```

### 3.2 对账决策

解析和对账结果不直接写账本，而是先生成决策对象：

```ts
ReconcileDecision {
  action: 'matched' | 'suspected' | 'new' | 'transfer' | 'ignore';
  row: BankRow;
  ledgerSyncId?: string;
  reason?: string;
  fromAccountName?: string;
  toAccountName?: string;
  suggestedCategoryName?: string;
}
```

用户确认后，才把 `new` / `transfer` 转成实际交易。

### 3.3 第一阶段不新增持久化流水表

第一阶段可以沿用导入 token / 内存缓存模式：

```text
upload bank statement
  → cache BankRow[]
  → reconcile preview
  → execute
  → token consume
```

这样避免先引入大表和生命周期管理。

第二阶段如果需要重新解析、审计、长期跟踪银行 statement，再引入：

```text
bank_statements
bank_statement_rows
transaction_source_refs
```

---

## 4. 银行文件解析

### 4.1 Provider adapter

每个银行单独实现 adapter：

| Provider | 文件 | 策略 |
|---|---|---|
| `icbc` | PDF | 提取表格 / 坐标文本，清理水印噪声 |
| `psbc` | PDF | 表格结构较清晰，提取标准表格列 |
| `cmb` | PDF | 无表格线，用记账日期作为行锚点，再按坐标拆列 |
| `ccb` | `.xls` | 读取工作表表头，映射字段 |

所有 adapter 输出同一个 `BankRow`。

### 4.2 招商银行 PDF 解析策略

招行 PDF 没有表格线，不能只依赖普通 `extract_text()` 按行读取。

推荐步骤：

1. 使用 PDF 文本层，而不是 OCR；
2. 用“记账日期”作为行锚点；
3. 根据坐标把日期、金额、余额、摘要、对手信息拆列；
4. 换行的对手信息归并到最近一条日期锚点；
5. 页眉、页脚、章、水印不作为业务数据；
6. 渲染页面图片做人工或自动交叉校验。

OCR 不作为第一阶段主数据源，只可作为辅助确认。如果 PDF 没有文本层，应返回错误，提示用户导出 Excel / CSV。

### 4.3 水印与页眉页脚

以下内容不进入业务数据：

- 二维码识别提示；
- 页眉 / 页脚；
- 章和背景水印；
- 页码；
- 温馨提示；
- 验真说明。

关键字段必须通过格式校验：

- 日期；
- 金额；
- 余额；
- 币种。

### 4.4 余额链校验

解析器应计算：

```text
prev balance + current amount == current balance ?
```

但结果只作为 warning，不作为导入失败条件。

原因：

- 真实银行 PDF 也可能存在结算口径问题；
- 脱敏 PDF 必然会导致余额链异常；
- 跨页、跨日结算、冲正、退款可能导致短期余额显示不平。

输出：

```json
{
  "balance_chain_warnings": [
    {
      "rowHash": "...",
      "expectedBalance": "333.25",
      "pdfBalance": "1333.25",
      "difference": "1000.00"
    }
  ]
}
```

---

## 5. 账户识别与别名归一

现有账本中可能存在复合账户名，例如：

```text
花呗
花呗&红包
余额宝&红包
招商银行储蓄卡(0932)&余额宝
```

银行对账前必须做账户别名归一化。

### 5.1 平台账户别名

示例：

```ts
花呗:
  - 花呗
  - 花呗&红包
  - 花呗&花呗青春特惠
  - 花呗&现金抵价券

微信零钱:
  - 零钱
  - 微信零钱

支付宝余额:
  - 余额
  - 账户余额
  - 支付宝余额

余额宝:
  - 余额宝
  - 余额宝&红包
  - 余额宝&红包&碰一下立减
```

### 5.2 银行账户别名

根据 `provider + accountRef` 映射：

```text
cmb + 6222****0932
  → 招商银行储蓄卡(0932)

psbc + 6217****8354
  → 中国邮政储蓄银行储蓄卡(8354)

icbc + 6212****3742
  → 工商银行储蓄卡(3742)

ccb + 6227****2892
  → 建设银行储蓄卡(2892)
```

如果账本中找不到对应账户，用户可以：

1. 创建新账户；
2. 映射到已有账户；
3. 取消导入。

### 5.3 还款目标账户

花呗还款目标账户优先级：

1. 已有规范账户 `花呗`；
2. 已有名称以 `花呗` 开头的账户；
3. 用户确认后创建 `花呗`。

信用卡还款目标账户：

1. 根据银行摘要或对手方识别信用卡；
2. 映射到已有信用卡账户；
3. 无法识别时进入确认页，由用户选择目标账户。

---

## 6. 对账算法

### 6.1 候选交易

从目标账本 snapshot 读取交易，并规范化：

```ts
LedgerCandidate {
  syncId: string;
  txType: 'expense' | 'income' | 'transfer';
  amount: Decimal;
  happenedAt: Date;
  note: string | null;
  categoryName: string | null;
  accountName: string | null;
  fromAccountName: string | null;
  toAccountName: string | null;
  tags: string[];
}
```

对账前统一：

- 金额保留两位小数；
- 时间使用账本存储时间，银行时间按东八区解释；
- 账户名做 alias 归一化；
- 忽略软删除交易。

### 6.2 基础匹配条件

银行行和账本交易必须同时满足：

```text
1. 金额绝对值一致；
2. 方向一致；
3. 时间差 <= 3 天；
4. 一条银行行最多匹配一条账本交易；
5. 一条账本交易最多被一条银行行匹配。
```

方向映射：

| 银行方向 | 可匹配账本交易 |
|---|---|
| 银行流出 | expense，或 transfer 的 from 是银行账户 |
| 银行流入 | income，或 transfer 的 to 是银行账户 |

### 6.3 评分

对满足基础条件的候选交易评分：

| 维度 | 权重 |
|---|---:|
| 时间差越小 | 高 |
| 对手方相似 | 高 |
| 支付方式 / 账户匹配 | 高 |
| 渠道相似 | 中 |
| 备注相似 | 中 |
| 分类相似 | 低 |

输出三类：

```text
matched      自动匹配成功
suspected    疑似匹配，需用户确认
unmatched    未匹配
```

### 6.4 matched 规则

满足以下任一条件可标记 `matched`：

```text
amount 一致 + direction 一致 + date diff = 0
```

或者：

```text
amount 一致
direction 一致
date diff <= 3 天
counterparty / note similarity 达到阈值
```

`matched` 默认不新建交易。

### 6.5 suspected 规则

满足基础条件但信息不明确时，进入 `suspected`：

- 同一天多笔同金额；
- 银行对手方只显示财付通 / 支付宝；
- 银行摘要只有“快捷支付”；
- 账本备注缺失或已脱敏；
- 多个候选分数接近。

用户必须选择：

```text
匹配某笔
跳过
新建
忽略
```

### 6.6 一对一分配

对账使用一对一分配，避免同一笔账本交易被多条银行行占用。

第一阶段可以使用贪心分配：

1. 生成所有候选；
2. 按分数排序；
3. 分数相同按时间差排序；
4. 已占用的 bank row / ledger tx 不再参与；
5. 分数不足的进入 `suspected`。

---

## 7. 平台交易特殊规则

银行对手方或摘要命中以下关键词时，进入平台规则：

```text
支付宝
蚂蚁
花呗
财付通
微信
零钱
余额宝
网商银行
```

### 7.1 普通消费已存在于账本

如果银行行能匹配到已有微信 / 支付宝支出：

```text
matched → skip
```

不新建。

### 7.2 平台充值 / 提现

以下场景生成 `transfer`：

```text
bank → 支付宝余额
bank → 微信零钱
bank → 余额宝
支付宝余额 → bank
微信零钱 → bank
余额宝 → bank
```

方向规则：

| 银行金额 | 推断方向 |
|---|---|
| `-` | bank → platform |
| `+` | platform → bank |

### 7.3 花呗 / 信用卡还款

还款不是 expense，也不是 income。

固定生成：

```text
bank account → Huabei / Credit Card account
```

示例：

```text
银行流水：
支付宝信贷业务待还款账户 -200.00

结果：
transfer
from = 招商银行储蓄卡(0932)
to   = 花呗
```

如果账本中已有“花呗”账户，则映射到该账户。
如果账本没有“花呗”账户，用户确认后创建。

### 7.4 平台退款

银行收入如果匹配到已有支出，优先标记为退款关联：

```text
matched refund
```

如果账本中没有对应退款记录，则可以新建：

```ts
{
  txType: 'income',
  categoryName: '退款',
  accountName: bankAccount
}
```

---

## 8. 新建交易规则

只有 `unmatched` 且非平台内部转账的银行行才创建新交易。

### 8.1 新建支出

适用：

- 银行直付消费；
- 代扣水电费；
- 物业费；
- 手续费；
- 银行卡 POS 消费。

```ts
{
  txType: 'expense',
  amount: abs(amount),
  happenedAt,
  accountName: bankAccount,
  note: summary + counterparty,
  categoryName: '未分类'
}
```

如果摘要明确，可建议分类：

| 银行摘要 | 建议分类 |
|---|---|
| 手续费 | 支出 / 手续费 |
| 水费 | 支出 / 水费 |
| 电费 | 支出 / 电费 |
| 燃气费 | 支出 / 燃气费 |
| 物业费 | 支出 / 物业费 |

### 8.2 新建收入

适用：

- 代发工资；
- 奖金；
- 银行利息；
- 他行汇入；
- 银行入账。

```ts
{
  txType: 'income',
  amount: abs(amount),
  happenedAt,
  accountName: bankAccount,
  note: summary + counterparty,
  categoryName: '未分类'
}
```

建议分类：

| 银行摘要 | 建议分类 |
|---|---|
| 代发工资 | 收入 / 工资 |
| 奖金 | 收入 / 奖金 |
| 利息 | 收入 / 利息 |
| 账户结息 | 收入 / 利息 |
| 退款 | 收入 / 退款 |

### 8.3 新建转账

适用：

- ATM 取款 / 存款；
- 本人银行间转账；
- 平台充值 / 提现；
- 花呗 / 信用卡还款。

```ts
{
  txType: 'transfer',
  amount: abs(amount),
  happenedAt,
  fromAccountName,
  toAccountName,
  note: summary + counterparty
}
```

常见方向：

| 场景 | from | to |
|---|---|---|
| ATM 取款 | 银行账户 | 现金 |
| ATM 存款 | 现金 | 银行账户 |
| 平台充值 | 银行账户 | 支付宝 / 微信 / 余额宝 |
| 平台提现 | 支付宝 / 微信 / 余额宝 | 银行账户 |
| 花呗还款 | 银行账户 | 花呗 |
| 信用卡还款 | 银行账户 | 信用卡 |

---

## 9. API 设计

### 9.1 上传银行流水

```http
POST /api/v1/bank-import/upload
Content-Type: multipart/form-data
```

请求字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `file` | file | 银行 PDF / XLS |
| `target_ledger_id` | string | 目标账本 external id |

响应：

```json
{
  "token": "bank_import_token",
  "provider": "cmb",
  "account_ref": "6222****0932",
  "row_count": 25,
  "warnings": [],
  "expires_at": "2026-10-11T09:00:00Z"
}
```

### 9.2 对账预览

```http
POST /api/v1/bank-import/{token}/reconcile
```

请求：

```json
{
  "target_ledger_id": "ledger_xxx",
  "account_mapping": {
    "bank": "招商银行储蓄卡(0932)",
    "huabei": "花呗",
    "wechat": "微信零钱",
    "alipay": "支付宝余额"
  }
}
```

响应：

```json
{
  "stats": {
    "total": 25,
    "matched": 12,
    "suspected": 5,
    "unmatched": 8,
    "transfer_suggested": 4
  },
  "decisions": [
    {
      "rowHash": "...",
      "action": "matched",
      "ledgerSyncId": "tx_xxx"
    },
    {
      "rowHash": "...",
      "action": "transfer",
      "fromAccountName": "招商银行储蓄卡(0932)",
      "toAccountName": "花呗"
    }
  ]
}
```

### 9.3 确认执行

```http
POST /api/v1/bank-import/{token}/execute
```

请求：

```json
{
  "target_ledger_id": "ledger_xxx",
  "decisions": [
    {
      "rowHash": "...",
      "action": "skip"
    },
    {
      "rowHash": "...",
      "action": "create_expense",
      "categoryName": "未分类",
      "accountName": "工商银行储蓄卡(3742)"
    },
    {
      "rowHash": "...",
      "action": "create_transfer",
      "fromAccountName": "招商银行储蓄卡(0932)",
      "toAccountName": "花呗"
    }
  ]
}
```

响应：

```json
{
  "created_tx_count": 8,
  "skipped_count": 17,
  "new_change_id": 12345
}
```

---

## 10. 前端流程

该能力涉及视觉和交互变化，实现前必须先做原型确认。

### 10.1 入口

建议在导入区域区分两个入口：

```text
导入账单
├─ 微信 / 支付宝 / BeeCount 账单
└─ 银行流水对账
```

### 10.2 上传页

用户输入：

1. 上传银行 PDF / XLS；
2. 查看自动识别银行；
3. 查看账号；
4. 选择目标账本；
5. 选择银行账户映射；
6. 选择花呗 / 微信 / 支付宝账户映射。

### 10.3 对账确认页

按状态分组：

```text
全部
matched
suspected
unmatched
transfer
warnings
```

每行展示：

| 银行日期 | 金额 | 摘要 | 对手方 | 建议动作 | 匹配账本交易 |
|---|---:|---|---|---|---|

用户可修改动作：

```text
跳过
新建支出
新建收入
创建转账
忽略
```

### 10.4 汇总确认页

展示：

```text
总行数
自动匹配跳过
疑似匹配待确认
计划新建支出
计划新建收入
计划创建转账
忽略
警告
```

用户点击“确认导入”后才执行。

### 10.5 完成页

展示：

```text
已跳过
已创建支出
已创建收入
已创建转账
警告
新 change id
```

---

## 11. 测试方案

### 11.1 测试原则

- 不使用真实默认账本；
- 不依赖脱敏 PDF 的金额做业务对账；
- 每次 reconciliation 测试新开账本；
- Mock 支付宝 / 微信数据先导入新账本；
- 脱敏银行 PDF 用于 parser 结构测试；
- Mock 银行流水用于对账语义测试。

### 11.2 Parser 测试

覆盖：

- 工行 PDF；
- 邮储 PDF；
- 招行 PDF；
- 建行 `.xls`。

验证：

- 页数；
- 行数；
- 日期；
- 金额；
- 余额；
- 摘要；
- 对手方；
- 页眉页脚剔除；
- 水印剔除；
- rawHash 稳定；
- 余额链 warning 不阻塞。

### 11.3 Mock 对账场景

新建测试账本，例如：

```text
Bank Reconciliation Test - 20261011
```

先导入 Mock 支付宝 / 微信账单。

覆盖以下场景：

#### A. 普通消费已存在

```text
微信账单：
2026-09-01 支出 35.00，支付方式 招商银行储蓄卡(0932)

银行流水：
2026-09-01 支出 35.00，摘要 快捷支付，对手 财付通
```

期望：

```text
matched → skip
```

#### B. 支付宝消费已存在

```text
支付宝账单：
2026-09-02 支出 126.00，支付方式 招商银行储蓄卡(0932)

银行流水：
2026-09-02 支出 126.00，摘要 快捷支付，对手 蚂蚁
```

期望：

```text
matched → skip
```

#### C. 时间差 3 天内

```text
微信账单：
2026-09-01 支出 80.00

银行流水：
2026-09-03 支出 80.00
```

期望：

```text
matched 或 suspected
```

#### D. 时间差超过 3 天

```text
微信账单：
2026-09-01 支出 90.00

银行流水：
2026-09-08 支出 90.00
```

期望：

```text
unmatched
```

#### E. 同日同金额多笔

```text
账本：
2026-09-01 支出 20.00，早餐
2026-09-01 支出 20.00，咖啡

银行流水：
2026-09-01 支出 20.00，快捷支付
```

期望：

```text
suspected
```

不允许自动乱配。

#### F. 花呗消费已存在，银行行是花呗还款

```text
支付宝账单：
2026-09-01 花呗消费 200.00
→ 已导入 expense，账户 花呗

银行流水：
2026-09-10 支付宝信贷业务待还款账户 -200.00
```

期望：

```text
transfer
from = bank
to   = 花呗
```

银行还款不匹配原始消费，也不新建 expense。

#### G. 信用卡还款

```text
银行流水：
招商银行信用卡 -500.00
```

期望：

```text
transfer
from = bank
to   = 招商银行信用卡
```

#### H. 平台提现

```text
银行流水：
财付通支付科技有限公司 +1000.00
```

期望：

```text
transfer
from = 微信零钱
to   = bank
```

#### I. 平台充值

```text
银行流水：
支付宝 -1000.00
```

期望：

```text
transfer
from = bank
to   = 支付宝余额
```

#### J. 工资

```text
银行流水：
代发工资 +10000.00
```

期望：

```text
income
```

#### K. 利息和手续费

```text
账户结息 +0.02
→ income

手续费 -10.00
→ expense
```

#### L. ATM

```text
ATM 取款 -1000.00
→ transfer：bank → cash

卡现金存 +1000.00
→ transfer：cash → bank
```

### 11.4 端到端测试流程

```text
1. 创建新账本；
2. 导入 Mock 微信账单；
3. 导入 Mock 支付宝账单；
4. 上传脱敏银行 PDF / XLS，验证 parser；
5. 用 Mock 银行流水生成对账预览；
6. 校验 matched / suspected / unmatched / transfer；
7. 用户确认；
8. 执行导入；
9. 校验账本交易数量和类型；
10. 校验真实默认账本未被修改。
```

---

## 12. 实施阶段

### Phase 0：原型确认

先产出前端原型：

- 上传页；
- 账户映射页；
- 对账确认页；
- 汇总确认页；
- 完成页。

用户确认原型后，再实现真实前端。

### Phase 1：Parser

实现：

```text
BankRow
bank provider adapter
rawHash
parse warnings
balance chain warning
```

覆盖：

- 工行 PDF；
- 邮储 PDF；
- 招行 PDF；
- 建行 `.xls`。

### Phase 2：离线对账

实现：

- 读取目标账本 snapshot；
- 账户 alias normalization；
- ±3 天匹配；
- 一对一分配；
- matched / suspected / unmatched；
- 平台充值提现识别；
- 花呗 / 信用卡还款识别。

先输出 JSON 报告，不写账本。

### Phase 3：API

实现：

- upload；
- reconcile；
- execute；
- token 过期；
- 权限校验；
- audit log。

### Phase 4：前端

按已确认原型实现：

- 银行流水上传；
- 账户映射；
- 对账确认；
- 执行进度；
- 结果页。

### Phase 5：灰度验证

使用隔离账本和小范围真实数据验证：

- parser；
- 对账；
- 生成报告；
- 不写真实账本。

确认后再开放完整功能。

---

## 13. 验收标准

功能验收必须满足：

1. 银行 PDF / XLS 能解析为标准 `BankRow`；
2. 脱敏金额不会导致解析失败；
3. 余额链异常只产生 warning；
4. 对账对象是目标账本已有交易；
5. 已匹配交易不会重复创建；
6. 花呗 / 信用卡还款固定创建 transfer；
7. 平台充值 / 提现固定创建 transfer；
8. 工资、利息、手续费、银行直付可创建收支；
9. 同金额多笔交易进入 suspected，不自动乱配；
10. 用户确认前不写账本；
11. 测试过程中默认账本不被修改；
12. 时间窗口固定 ±3 天，不做用户配置；
13. 执行结果可审计；
14. 前端使用 HeroUI，并遵循已确认原型。
