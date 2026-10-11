# 账单导入与财务事件模型方案

- 状态：Design / Approved direction
- 范围：支付宝、微信、BeeCount 账单导入，退款 / 交易关闭 / 不计收支 / 内部转账的财务事件建模与统计口径
- 相关文档：
  - `docs/design/sqlite-postgres-orm-alembic.md`
  - `docs/ORM_RULES.md`

---

## 1. 目标

当前导入器只根据 `收/支` 判断：

```text
支出       -> expense
收入       -> income
不计收支    -> 忽略
/          -> 忽略
```

这会导致：

1. 支付宝 `交易关闭` 支出被导入。
2. 对应 `退款成功` 记录被忽略。
3. 微信已退款支出和退款收入同时进入统计。
4. 无法追溯订单生命周期。
5. 月度、年度、分类、净资产统计被污染。

目标：

```text
所有账单行都保留；
不把 `不计收支` 直接当“无意义”丢弃；
区分真实收支、退款、内部转账、还款、理财、关闭订单；
收支统计默认使用净额口径。
```

---

## 2. 核心原则

`不计收支` 不是业务类型，而是支付宝/微信给出的统计口径标记。

它的准确含义是：

```text
这一行不计入收入/支出统计
```

它可能代表：

```text
退款成功
信用卡还款
投资理财
账户充值 / 提现
交易关闭
失败订单
内部账户转移
```

因此导入器不能写：

```python
if raw_type in {"不计收支", "/"}:
    return None
```

而应该：

1. 保留原始行；
2. 根据 `交易分类`、`交易状态`、`交易对方`、金额等推导 `event_kind`；
3. 只有真实 `expense` / `income` 影响收支统计；
4. 其他事件保留为订单或资产生命周期数据。

---

## 3. 数据模型

分三层。

---

## 3.1 Layer 1：原始导入记录

表：

```text
imported_records
```

所有账单行无条件保留。

| 字段 | 说明 |
|---|---|
| `id` | UUID |
| `user_id` | 用户 |
| `import_file_id` | 上传文件 |
| `source_type` | `alipay` / `wechat` / `beecount` / `generic` |
| `source_row_number` | 原始行号 |
| `raw_payload` | 原始字段 JSON |
| `source_hash` | 稳定 hash |
| `parse_status` | `parsed` / `failed` / `duplicate` |
| `error_message` | 解析错误 |
| `created_at` | 创建时间 |

作用：

```text
保证可审计；
支持重新解析；
避免静默丢失；
支持排查退款 / 交易关闭问题。
```

---

## 3.2 Layer 2：标准化财务事件

表：

```text
financial_events
```

字段建议：

| 字段 | 说明 |
|---|---|
| `id` | UUID |
| `user_id` | 用户 |
| `ledger_id` | 账本 |
| `source_record_id` | 指向原始导入记录 |
| `source_type` | `alipay` / `wechat` / `beecount` |
| `event_kind` | 标准事件类型 |
| `source_status` | 原始状态 |
| `direction` | `outflow` / `inflow` / `internal` / `none` |
| `amount` | 原始金额，正数 |
| `currency` | 币种 |
| `gross_amount` | 原始金额 |
| `refund_amount` | 退款金额 |
| `net_amount` | 净额 |
| `related_event_id` | 关联事件 |
| `account_name` | 原始账户 |
| `account_id` | 账户 ID |
| `category_name` | 原始分类 |
| `category_id` | 分类 ID |
| `happened_at` | 交易时间 |
| `trade_order_id` | 交易订单号 |
| `merchant_order_id` | 商家订单号 |
| `source_payload` | 原始 JSON |
| `created_at` / `updated_at` | 时间 |

---

## 3.3 Layer 3：账本事务 / 同步

现有账本继续使用：

```text
sync_changes
read_tx_projection
snapshot
```

只有真实 `expense` / `income` 事件进入现有账本事务流。

退款、内部转账、还款、理财、关闭订单保留在 `financial_events`，不进入普通收支统计。

---

## 4. 支付宝状态映射

支付宝字段：

```text
交易时间
交易分类
交易对方
对方账号
商品说明
收/支
金额
收/付款方式
交易状态
交易订单号
商家订单号
备注
```

---

## 4.1 支出

`收/支 = 支出`

| `交易状态` | `event_kind` | 计入支出 |
|---|---|---|
| `交易成功` | `expense` | 是 |
| `支付成功` | `expense` | 是，金额为 0 也保留 |
| `交易关闭` | `order_closed` | 否 |

---

## 4.2 收入

`收/支 = 收入`

| `交易状态` | `event_kind` | 计入收入 |
|---|---|---|
| `交易成功` | `income` | 是 |
| `交易关闭` | `order_closed` | 否 |

---

## 4.3 不计收支

`收/支 = 不计收支` 不能统一跳过，需要结合分类和状态。

| 分类 / 场景 | `交易状态` | `event_kind` | 说明 |
|---|---|---|---|
| 退款 | `退款成功` | `refund` | 冲减原支出 |
| 信用借还 | `还款成功` | `repayment` | 资产 / 负债转移 |
| 信用借还 | `还款失败` | `failed_repayment` | 保留失败记录 |
| 投资理财 | `交易成功` | `investment` | 资产转移 |
| 账户存取 | `交易成功` | `transfer` | 账户内部转移 |
| 任意 | `交易关闭` | `order_closed` | 不计入收支 |

---

## 5. 微信状态映射

微信字段：

```text
交易时间
交易类型
交易对方
商品
收/支
金额(元)
支付方式
当前状态
交易单号
商户单号
备注
```

---

## 5.1 支出

`收/支 = 支出`

| `当前状态` | `event_kind` | 计入支出 |
|---|---|---|
| `支付成功` | `expense` | 是 |
| `对方已收钱` | `expense` | 是 |
| `已转账` | `expense` / `transfer` | 是，视账户模型 |
| `已全额退款` | `refunded_expense` | 初始保留，净支出为 0 |
| `已退款(¥xx)` | `partially_refunded_expense` | 净支出 = 原金额 - 退款金额 |
| `对方已退还` | `refunded_expense` | 初始保留，净支出为 0 |

---

## 5.2 收入

`收/支 = 收入`

| `当前状态` | `event_kind` | 计入收入 |
|---|---|---|
| `已存入零钱` | `income` | 是 |
| `已到账` | `income` | 是 |
| `已收钱` | `income` | 是 |
| `已全额退款` | `refund` | 否 |
| `已退款(¥xx)` | `refund` | 否 |

---

## 5.3 中性交易

`收/支 = /`

| 类型示例 | `event_kind` | 说明 |
|---|---|---|
| 转入零钱通-来自零钱 | `internal_transfer` | 零钱 → 零钱通 |
| 转入零钱通-来自银行卡 | `transfer` | 银行 → 零钱通 |
| 零钱通转出-到零钱 | `internal_transfer` | 零钱通 → 零钱 |
| 零钱充值 | `transfer` | 银行 → 零钱 |
| 零钱提现 | `transfer` | 零钱 → 银行 |

---

## 6. 退款处理

退款不是收入。

退款应建模为：

```text
refund event
```

并尽量关联原支出。

---

## 6.1 完全退款

```text
原支出：168
退款：168
```

正确表达：

```text
expense.gross_amount = 168
expense.refund_amount = 168
expense.net_amount = 0

refund.related_event_id = expense.event_id
```

统计：

```text
gross_expense += 168
refund += 168
net_expense += 0
```

---

## 6.2 部分退款

```text
原支出：500
退款：100
```

正确表达：

```text
expense.gross_amount = 500
expense.refund_amount = 100
expense.net_amount = 400
```

---

## 6.3 交易关闭 + 退款成功

支付宝常见：

```text
03-05 支出 22749，交易关闭
03-06 不计收支 22749，退款成功
```

正确结果：

```text
两者都不进入 expense；
两者都保留为 order / refund lifecycle event；
净支出为 0。
```

---

## 7. 退款匹配规则

---

## 7.1 支付宝

支付宝有稳定订单字段：

```text
交易订单号
商家订单号
```

匹配优先级：

1. `交易订单号` 完全匹配；
2. `商家订单号` 完全匹配；
3. 金额 + 交易对方 + 收/付款方式 + 时间窗口辅助匹配；
4. 无法匹配标记：

```text
match_status = unmatched_refund
```

允许前端人工关联。

---

## 7.2 微信

微信原支出和退款收入的交易单号可能不同，商户单号也不一定稳定。

自动匹配建议：

1. 金额相同；
2. 支付方式相同；
3. 交易对方相同；
4. 退款时间晚于原支出；
5. 时间窗口有限；
6. 状态或交易类型包含退款；
7. 交易类型通常包含 `-退款`。

无法自动匹配时：

```text
match_status = unmatched_refund
```

前端允许人工关联。

---

## 8. 统计口径

系统应同时支持两种口径。

---

## 8.1 流水口径

展示原始生命周期：

```text
gross_expense
gross_income
refund
internal_transfer
repayment
investment
order_closed
failed_order
```

---

## 8.2 净额口径

首页默认使用净额：

```text
net_expense = gross_expense - refund_offset
net_income = gross_income - income_refund_offset
net_balance = net_income - net_expense
```

退款不作为 income。

---

# 9. API 设计

---

## 9.1 导入预览

`POST /api/v1/import/{token}/preview`

返回：

```json
{
  "total_rows": 1846,
  "parsed_rows": 1846,
  "failed_rows": 0,
  "groups": {
    "expense": 1537,
    "income": 95,
    "refund": 46,
    "internal_transfer": 137,
    "repayment": 18,
    "order_closed": 38,
    "failed_order": 2,
    "unknown": 0
  }
}
```

每行可返回：

```json
{
  "source_row_number": 24,
  "source_status": "退款成功",
  "event_kind": "refund",
  "pnl_affects": false,
  "amount": "22749.00"
}
```

---

## 9.2 执行导入

`POST /api/v1/import/{token}/execute`

行为：

1. 写入 `imported_records`；
2. 写入 `financial_events`；
3. 只有 `expense` / `income` 进入现有账本事务；
4. 退款 / 转账 / 订单事件保留在 `financial_events`；
5. 生成 `sync_changes`；
6. 更新 projections；
7. 写入 audit log。

---

## 9.3 统计接口

`GET /api/v1/read/workspace/analytics`

返回：

```json
{
  "gross_expense": 17217.28,
  "expense_refund": 956.95,
  "net_expense": 16260.33,
  "gross_income": 6904.64,
  "income_refund": 956.95,
  "net_income": 5947.69,
  "net_balance": -10312.64
}
```

数字为示例。

---

## 9.4 事件查询

新增或扩展：

```text
GET /api/v1/read/financial-events
GET /api/v1/read/financial-events/{id}
GET /api/v1/read/refunds
GET /api/v1/read/order-events
```

支持筛选：

```text
event_kind
source_status
ledger_id
account_id
happened_at
matched / unmatched
```

---

# 10. 前端设计

---

## 10.1 导入预览页

导入预览不再显示“跳过不计收支”，而是分组显示：

```text
实际支出
实际收入
退款
内部转账
还款
理财
交易关闭
失败订单
未知
```

每行显示：

```text
原始状态
推导事件类型
金额
是否计入收支
```

---

## 10.2 交易列表页

交易行增加标签：

```text
已退款
部分退款
交易关闭
内部转账
还款
理财
```

退款交易展开显示：

```text
原金额
退款金额
净金额
关联退款
```

---

## 10.3 首页

首页默认展示净额：

```text
本月收入
本月支出
本月结余
```

并提供切换：

```text
净额口径 / 流水口径
```

---

## 10.4 订单 / 事件页

新增“账单事件”或“订单流水”页面，用于查看：

```text
退款
交易关闭
还款
内部转账
理财
失败订单
```

这些不进入收支统计，但用户可以查。

---

# 11. 数据迁移

---

## 11.1 新增 Alembic migration

新增表：

```text
import_files
imported_records
financial_events
event_links
```

并扩展 transaction payload / projection 字段：

```text
source_event_id
source_type
source_status
event_kind
gross_amount
refund_amount
net_amount
related_event_id
```

---

## 11.2 当前 PostgreSQL 数据修复

当前 PostgreSQL 中已经导入了错误数据。不要直接物理删除，建议：

1. 备份 `pg_dump` 和账本 snapshot；
2. 扫描当前账本中的支出 / 收入；
3. 根据金额、时间、分类、账户、订单号匹配原始账单；
4. 将 `交易关闭` 交易标记：

```sql
exclude_from_stats = true
source_status = '交易关闭'
event_kind = 'order_closed'
```

5. 对已退款交易：

```sql
refund_amount = 退款金额
net_amount = gross_amount - refund_amount
```

6. 补充 `financial_events`；
7. 生成 correction sync change；
8. 重新计算 projections。

---

## 11.3 当前 `¥22,749` 案例处理

当前数据库中：

```text
03-05 19:38:17  支出 22749  交易关闭
03-08 17:47:34  支出 22749  交易成功
```

正确处理：

```text
03-05 交易关闭：
exclude_from_stats = true
source_status = 交易关闭
event_kind = order_closed

03-08 交易成功：
保留为真实 expense
```

如果确认两笔属于同一业务订单的关闭和重新支付，则建立关联：

```text
relation_type = replacement
```

---

## 11.4 SQLite 现有数据

SQLite 现有数据可以选择：

1. 不迁移，作为旧本地数据保留；
2. 或基于新的导入模型重新导入；
3. 不建议静默改写用户已有 SQLite 文件。

---

# 12. 后端改造点

主要涉及：

```text
src/models.py
src/services/import_data/schema.py
src/services/import_data/parser.py
src/services/import_data/parsers/generic.py
src/services/import_data/transformer.py
src/services/import_data/cache.py
src/services/import_data/event_inference.py 新增
src/routers/import_data/endpoints.py
src/routers/read/workspace.py
src/routers/read/summary.py
src/projection.py
src/snapshot_builder.py
```

Alembic 新增：

```text
alembic/versions/xxx_import_financial_events.py
```

---

# 13. 测试计划

---

## 13.1 状态映射测试

覆盖支付宝状态：

```text
交易成功
交易关闭
退款成功
还款成功
还款失败
支付成功
```

覆盖微信状态：

```text
支付成功
对方已收钱
对方已退还
已全额退款
已退款(¥xx)
已存入零钱
已到账
已收钱
资金已到账
充值完成
提现已到账
```

---

## 13.2 导入测试

覆盖：

```text
Alipay CSV
WeChat XLSX
BeeCount CSV
重复导入
部分退款
全额退款
跨月退款
无法匹配退款
```

---

## 13.3 数据库测试

SQLite 和 PostgreSQL 分别验证：

```bash
alembic upgrade head
alembic check
```

---

## 13.4 API smoke test

PostgreSQL 环境验证：

```text
注册
登录
创建账本
导入账单
sync push
sync pull
read workspace
read analytics
read transactions
```

---

# 14. 验收标准

1. 所有账单行保留。
2. `不计收支` 不再静默跳过。
3. 支付宝和微信状态分别建模。
4. 退款不再作为收入。
5. 交易关闭不再作为支出。
6. 完全退款交易的净支出为 0。
7. 部分退款交易统计为净支出。
8. 首页默认展示净额，可切换流水口径。
9. PostgreSQL 和 SQLite 均可完成导入。
10. 现有 PostgreSQL 数据提供可审计 reconciliation 方案。
