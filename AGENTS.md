# BeeCount Cloud — Agent 全局规范

本文件只保留仓库级全局约束和设计文档索引。  
具体实现规则放在对应专题文档中，不要在本文件重复展开。

---

## 1. 全局约束

| 主题 | 必须遵守 |
|---|---|
| 语言 / 框架 | 后端使用 Python / FastAPI / SQLAlchemy 2.x；前端使用 React / Vite |
| 数据库 | 业务代码必须兼容 SQLite 和 PostgreSQL |
| schema 变更 | 所有结构变更必须通过 Alembic migration，不得使用 `create_all()` 初始化生产数据 |
| 事件导入 | 支付宝 / 微信账单中的退款、交易关闭、还款、理财、内部转账必须保留为财务事件，不得静默跳过 |
| 收支统计 | 默认统计“实际净额”；退款不是收入，交易关闭不是支出 |
| 文档 | 涉及数据库、导入、退款、Alembic 的修改，必须同步检查或更新对应设计文档 |
| 代码格式 | 禁止运行 prettier、eslint --fix、gofmt 等仓库级格式化操作，避免无关 diff |
| 测试 | 数据库 schema、导入状态映射、退款配对改动必须补测试，并在合并前运行相关测试 |

---

## 2. 设计文档索引

| 主题 | 文档 | 何时阅读 |
|---|---|---|
| ORM / Alembic 规范 | [`docs/ORM_RULES.md`](./docs/ORM_RULES.md) | 修改模型、查询、事务、Alembic migration 前 |
| SQLite / PostgreSQL 兼容方案 | [`docs/design/sqlite-postgres-orm-alembic.md`](./docs/design/sqlite-postgres-orm-alembic.md) | 修改双库兼容、Alembic migration、PG / SQLite行为前 |
| 账单导入与财务事件模型 | [`docs/design/import-financial-events.md`](./docs/design/import-financial-events.md) | 修改支付宝、微信、BeeCount 账单导入，退款，交易关闭，收支统计前 |
| 数据库迁移说明 | [`docs/MIGRATION.md`](./docs/MIGRATION.md) | 处理部署迁移、升级回滚说明时 |
| 同步架构 | [`docs/SYNC_ARCHITECTURE.md`](./docs/SYNC_ARCHITECTURE.md) | 修改 sync、projection、snapshot 相关逻辑时 |

---

## 3. 修改顺序要求

1. 先阅读本文件的“全局约束”；
2. 根据改动主题阅读“设计文档索引”中的对应文档；
3. 数据库修改必须同步考虑 SQLite / PostgreSQL；
4. 涉及账单导入时，必须说明每类原始状态最终映射为什么事件；
5. 完成后运行相关测试和 migration 检查。
