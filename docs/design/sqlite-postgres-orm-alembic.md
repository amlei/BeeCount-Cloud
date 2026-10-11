# SQLite / PostgreSQL 与 ORM / Alembic 兼容方案

- 状态：Design / Approved direction
- 范围：SQLAlchemy ORM、Alembic migration、SQLite 与 PostgreSQL 双库兼容
- 相关文档：
  - `docs/ORM_RULES.md`
  - `docs/design/import-financial-events.md`

---

## 1. 目标

BeeCount Cloud 需要同时支持：

```text
SQLite：默认本地 / 轻量部署
PostgreSQL 16：开发验证和生产部署
```

目标不是让 PostgreSQL 迁就 SQLite 的非法写法，而是让业务模型、数据库操作和 Alembic migration 都遵循 SQLAlchemy 官方的可移植写法。

---

## 2. 当前问题

### 2.1 Alembic migration

历史 migration 曾使用 PostgreSQL 不兼容的 boolean 默认值：

```python
server_default=sa.text("0")
server_default=sa.text("1")
```

SQLite 可以接受，PostgreSQL 会报：

```text
column "..." is of type boolean but default expression is of type integer
```

此外，部分 revision id 超过 PostgreSQL `alembic_version.version_num VARCHAR(32)` 的默认长度。SQLite 不强制 varchar 长度，PostgreSQL 会强制。

### 2.2 ORM / 查询层

项目大部分读写已经使用 SQLAlchemy ORM，但仍有几个需要显式处理的位置：

- SQLite 与 PostgreSQL 的 upsert 必须使用各自 dialect 的 insert 构造器。
- PostgreSQL 的 JSON 列不能直接 `LENGTH(payload_json)`，必须 `CAST(... AS TEXT)`。
- SQLite 专属 `PRAGMA` 必须限制在 SQLite engine 分支内。

### 2.3 运行环境

SQLite 文件不应部署在不可靠的共享文件系统上。Apple Container / macOS VirtioFS bind mount 可能导致 SQLite WAL 的共享内存和文件锁语义不稳定。PostgreSQL 不存在该问题。

---

## 3. 核心原则

### 3.1 ORM 是默认访问层

业务代码优先使用：

```python
select()
Session
sessionmaker
relationship
```

避免随手拼 SQL。

需要数据库特有能力时，必须显式声明数据库分支。

---

### 3.2 schema 使用可移植类型

推荐：

```python
String(length)
Text
Boolean
DateTime(timezone=True)
BigInteger().with_variant(Integer(), "sqlite")
JSON
```

注意事项：

| 类型 | SQLite | PostgreSQL | 规则 |
|---|---|---|---|
| `Boolean` | 接受 `0/1` | 只接受 `true/false` | server default 必须用 `sa.false()` / `sa.true()` |
| `DateTime` | 弱时区语义 | 有 timezone | 需要时区语义时使用 `DateTime(timezone=True)` |
| `BigInteger` | 不一定 autoincrement | 原生支持 | 需要 SQLite autoincrement 时使用 `with_variant` |
| `JSON` | TEXT 存储 | JSON 类型 | 默认用通用 JSON；确需 PG JSONB 时再显式评估 |

---

### 3.3 server default

Boolean 一律使用：

```python
sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())

sa.Column(
    "encrypted",
    sa.Boolean(),
    nullable=False,
    server_default=sa.false(),
)
```

禁止：

```python
server_default=sa.text("0")
server_default=sa.text("1")
```

Integer / String 的 server default 可以写成：

```python
server_default="0"
server_default="30"
server_default="running"
server_default="[]"
```

或使用 `sa.text()`，但必须确认 SQLite 与 PostgreSQL 都能接受。

---

### 3.4 Upsert

SQLite 和 PostgreSQL 都有 `ON CONFLICT`，但 SQLAlchemy 的方言构造器不同。

正确结构：

```python
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def dialect_insert(dialect_name: str):
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    return None
```

禁止把 `sqlite.insert` 构造出的 statement 直接交给 PostgreSQL 执行。

---

## 4. Alembic 规则

### 4.1 revision id

Alembic 默认 `alembic_version.version_num` 是 `VARCHAR(32)`。

因此所有 revision id 必须满足：

```text
len(revision) <= 32
```

不允许依赖 SQLite 忽略 varchar 长度。

### 4.2 migration 编写规则

- 所有 schema 变更走 Alembic。
- 不使用 `Base.metadata.create_all()` 初始化生产数据库。
- 每个migration只表达一个清晰语义。
- `down_revision` 链必须有效，且只有一个 head。
- 已应用的 migration 不应随意修改；只有历史 migration 在某个目标数据库从未成功执行时，才允许做兼容性修复，并必须说明影响。
- SQLite 不支持的 ALTER 操作使用 Alembic batch mode。

示例：

```python
with op.batch_alter_table("read_tx_projection") as batch_op:
    batch_op.alter_column(
        "tx_type",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
```

### 4.3 环境配置

`alembic/env.py` 应启用：

```python
compare_type=True
compare_server_default=True
render_as_batch=connection.dialect.name == "sqlite"
transaction_per_migration=True
```

这样可以更可靠地发现 models 与数据库结构之间的漂移。

---

## 5. 双数据库验证

每次影响 schema、查询或 migration 的修改，必须验证：

```bash
# SQLite
DATABASE_URL='sqlite:///./beecount-sqlite-check.db' alembic upgrade head
DATABASE_URL='sqlite:///./beecount-sqlite-check.db' alembic check

# PostgreSQL
DATABASE_URL='postgresql+psycopg://...' alembic upgrade head
DATABASE_URL='postgresql+psycopg://...' alembic check
```

对应 API smoke test 至少覆盖：

```text
/ready
注册 / 登录
创建账本
sync push
sync pull
read workspace
read analytics
read transactions
```

---

## 6. 存储部署规则

### SQLite

适合：

```text
单用户
轻量自托管
本地开发
无高并发写入需求
```

注意事项：

- 使用 WAL 前必须确认文件系统能稳定支持共享内存和文件锁。
- 不要把 SQLite 数据库放在 NFS / SMB / 不可靠 VirtioFS bind mount 上。
- Apple Container 场景优先使用 ext4 named volume。

### PostgreSQL

适合：

```text
多人使用
容器化部署
高并发写入
长期数据增长
需要更强事务和迁移可靠性
```

推荐 PostgreSQL 16 或更高版本。

---

## 7. 验收标准

方案视为完成的条件：

1. 全新 SQLite 空库可以 `alembic upgrade head`。
2. 全新 PostgreSQL 空库可以 `alembic upgrade head`。
3. 两边 `alembic check` 无漂移。
4. PostgreSQL 中 boolean 默认值不再是 integer。
5. 所有 Alembic revision id 长度不超过 32。
6. 运行时 PG upsert 使用 PostgreSQL dialect。
7. PG JSON 长度统计使用显式 cast。
8. SQLite 和 PostgreSQL 的核心 API smoke test 均通过。
