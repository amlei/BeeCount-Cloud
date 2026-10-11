# ORM / Alembic 规范

本文是数据库、ORM、Alembic 和双数据库兼容修改的强制规范。  
SQLite / PostgreSQL 兼容设计见 `docs/design/sqlite-postgres-orm-alembic.md`。

---

## 1. 范围

适用于：

```text
src/models.py
src/database.py
src/services/**
src/routers/**
alembic/**
tests/**
```

中所有涉及数据库结构、查询、事务、迁移的修改。

---

## 2. SQLAlchemy 规则

---

## 2.1 使用 SQLAlchemy 2.x

模型必须使用 SQLAlchemy 2.x 风格：

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
```

不新增旧式 `Column()` 模型。

---

## 2.2 Session 使用

数据库操作使用现有：

```python
SessionLocal
get_db()
```

规则：

- 请求内通过 `Depends(get_db)` 获取 Session；
- service 层接收 `Session`，不要自行创建 engine；
- 长任务使用独立 Session，并在 `finally` 中关闭；
- 一个业务事务内不要混用多个 Session 写同一批数据；
- 不要依赖 `expire_on_commit=True` 后继续访问过期属性。

---

## 2.3 查询规则

优先使用 SQLAlchemy 2.x：

```python
from sqlalchemy import select

user = db.scalar(select(User).where(User.email == email))
```

禁止为普通 ORM 可表达的查询手写字符串 SQL。

---

## 3. 可移植类型规则

SQLite 和 PostgreSQL 都必须可用。

| 场景 | 推荐写法 | 禁止 |
|---|---|---|
| 字符串 | `String(length)` | 无长度且依赖 SQLite 忽略长度 |
| 时间 | `DateTime(timezone=True)` | 存本地 naive 时间且无法解释时区 |
| 大整数主键 / cursor | `BigInteger().with_variant(Integer(), "sqlite")` | SQLite 下无法自增 |
| JSON | `JSON` | 没有明确需求时过早使用 PG JSONB |
| boolean default | `server_default=sa.false()` / `sa.true()` | `sa.text("0")` / `sa.text("1")` |
| 索引 / 约束 | 显式稳定命名 | 依赖 SQLite 弱约束或随机名 |

---

## 4. SQLite / PostgreSQL 分支规则

### 4.1 SQLite 专属配置

SQLite 的：

```text
PRAGMA journal_mode=WAL
PRAGMA busy_timeout
PRAGMA synchronous
PRAGMA foreign_keys
PRAGMA cache_size
```

只能放在 SQLite engine/listener 分支内。

PostgreSQL 不执行这些 PRAGMA。

---

### 4.2 Upsert

SQLite 和 PostgreSQL 都有 `ON CONFLICT`，但 SQLAlchemy 方言构造器不同：

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

禁止：

```python
# SQLite dialect 构造器直接给 PG 用
sqlite_insert(model)
```

---

### 4.3 PostgreSQL JSON

PostgreSQL 的 JSON 不能直接：

```sql
LENGTH(payload_json)
```

应使用：

```sql
LENGTH(CAST(payload_json AS TEXT))
```

或 SQLAlchemy：

```python
func.length(cast(Model.payload_json, Text))
```

---

## 5. 原生 SQL 规则

原生 SQL 只允许用于：

1. SQLAlchemy 无法表达的数据库特有能力；
2. 数据库维护命令，例如 `VACUUM`；
3. Alembic 数据回填中双方语义一致且必须执行的操作。

规则：

- 使用 `text()` 和绑定参数；
- 不拼接用户输入；
- 不写只兼容 SQLite 的语法；
- 如果 PG / SQLite 需要不同 SQL，必须显式判断 `dialect.name`；
- 说明为什么不能用 ORM。

---

## 6. Alembic 规则

---

## 6.1 migration 必须完整

每个 migration 必须包含：

```python
revision = "..."
down_revision = "..."
branch_labels = None
depends_on = None


def upgrade() -> None:
    ...


def downgrade() -> None:
    ...
```

`downgrade` 不强求恢复数据，但必须能回滚 schema；确实不可逆时必须写明原因。

---

## 6.2 revision id

Alembic 默认：

```text
alembic_version.version_num VARCHAR(32)
```

因此：

```text
len(revision) <= 32
len(down_revision) <= 32
```

不要依赖 SQLite 忽略 varchar 长度。

---

## 6.3 boolean default

Boolean 默认值必须使用：

```python
server_default=sa.false()
server_default=sa.true()
```

禁止：

```python
server_default=sa.text("0")
server_default=sa.text("1")
```

PostgreSQL 会报：

```text
column ... is of type boolean but default expression is of type integer
```

---

## 6.4 SQLite batch mode

SQLite 不支持很多直接 `ALTER COLUMN` 操作。

涉及修改列、约束或复杂变更时使用：

```python
with op.batch_alter_table("table_name") as batch_op:
    batch_op.alter_column(...)
```

Alembic 环境应启用：

```python
render_as_batch=connection.dialect.name == "sqlite"
```

---

## 6.5 数据回填

数据回填优先使用：

```python
connection.execute(sa.text(statement), params)
```

并保证 SQL 在 SQLite 和 PostgreSQL 上语义一致。

如果必须分库处理，显式写：

```python
if bind.dialect.name == "postgresql":
    ...
elif bind.dialect.name == "sqlite":
    ...
```

---

## 7. 必须运行的验证

影响数据库或 migration 时，至少验证：

```bash
# SQLite
DATABASE_URL='sqlite:///./beecount-sqlite-check.db' alembic upgrade head
DATABASE_URL='sqlite:///./beecount-sqlite-check.db' alembic check

# PostgreSQL
DATABASE_URL='postgresql+psycopg://...' alembic upgrade head
DATABASE_URL='postgresql+psycopg://...' alembic check
```

并运行相关 pytest。

---

## 8. Review checklist

提交前检查：

- [ ] 没有新增 SQLite-only DDL；
- [ ] boolean default 不使用 integer；
- [ ] 时间字段带 timezone；
- [ ] revision id 长度不超过 32；
- [ ] Alembic 单 head；
- [ ] `upgrade head` 在 SQLite 和 PostgreSQL 均成功；
- [ ] `alembic check` 无漂移；
- [ ] 方言分支只包含必须差异；
- [ ] 没有使用仓库级 format 工具重排无关代码。
