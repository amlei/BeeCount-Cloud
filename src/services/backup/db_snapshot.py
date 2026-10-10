"""Dialect-aware consistent database snapshots.

SQLite uses ``VACUUM INTO``; WAL mode means copying the main database file is
not transactionally safe. PostgreSQL uses the custom ``pg_dump`` format, which
is compressed and restoreable with ``pg_restore`` while the API remains online.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from sqlalchemy import MetaData, Table, delete, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def create_database_snapshot(db: Session, target_path: str | Path) -> Path:
    """Create a consistent business-database snapshot at ``target_path``."""
    engine = db.get_bind()
    dialect = engine.dialect.name
    if dialect == "sqlite":
        vacuum_into(db, target_path)
        return Path(target_path)
    if dialect == "postgresql":
        return pg_dump(engine, target_path)
    raise RuntimeError(f"database snapshots are not supported for {dialect}")


def pg_dump(engine: Engine | Connection, target_path: str | Path) -> Path:
    """Create a PostgreSQL custom-format dump using the engine connection URL."""
    # Session.get_bind() is typed as Engine | Connection; pg_dump only needs its
    # connection URL, so unwrap a bound Connection when one was supplied.
    if isinstance(engine, Connection):
        engine = engine.engine

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    url = engine.url
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(target),
        "--dbname",
        url.database or "",
    ]
    if url.host:
        command.extend(["--host", url.host])
    if url.port:
        command.extend(["--port", str(url.port)])
    if url.username:
        command.extend(["--username", url.username])

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pg_dump is not installed in this environment") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_dump timed out after 600 seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown pg_dump error").strip()
        raise RuntimeError(f"pg_dump failed: {detail[-1000:]}")
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("pg_dump did not produce a non-empty dump")
    logger.info("pg_dump completed: %s (%d bytes)", target, target.stat().st_size)
    return target


# 备份默认排除的"运维类"表 — 不属于用户数据,留着只让 tar 变大 + 暴露
# 内部细节:
#   - backup_runs / backup_run_targets:备份运行历史(restore 后没意义)
#   - sync_push_idempotency:24 小时滚动 idempotency 缓存
#   - audit_logs:管理员操作日志,运维痕迹,不属于"账本数据"
#   - refresh_tokens:登录 session,restore 后所有人都得重登,留着没用
#   - mcp_call_logs:MCP tool 调用审计,30 天滚动遥测,跟账本数据无关
# **PAT 表 (personal_access_tokens) 要保留** — 用户的 LLM 客户端配置依赖
# 这些 token,restore 后 LLM 仍然能连上,不用重新发 token。
# 用户数据相关(必须保留):users / user_profiles / devices / ledgers /
# sync_changes / sync_cursors / read_*_projection / attachment_files /
# personal_access_tokens / backup_remotes / backup_schedules /
# backup_schedule_remotes(配置要保留)
DEFAULT_EXCLUDED_TABLES = (
    "backup_runs",
    "backup_run_targets",
    "sync_push_idempotency",
    "audit_logs",
    "refresh_tokens",
    "mcp_call_logs",
)


def vacuum_into(
    db: Session,
    target_path: str | Path,
    *,
    exclude_tables: tuple[str, ...] | None = DEFAULT_EXCLUDED_TABLES,
) -> None:
    """跑 VACUUM INTO,把当前数据库一致快照写到 target_path。

    exclude_tables 提供时,VACUUM 完后开 copy 文件,**DELETE 这些表的数据**
    (保留 schema)+ 再 VACUUM 一次释放空间。default 排除运维类表
    (backup_runs / audit_logs 等),用户数据全部保留。

    **注意**:之前版本是 `DROP TABLE` 整张表,restore 后 server 启动会撞
    "no such table" 因为代码里有引用(典型:`mcp_call_logs`)。
    改成 `DELETE FROM` 仅清数据,schema 保留 → restore 后即插即用。

    target_path 父目录必须已存在 + 文件不能已存在(SQLite 要求)。
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    # VACUUM INTO is SQLite maintenance syntax and accepts a bound filename;
    # there is no portable ORM equivalent for a consistent SQLite snapshot.
    db.execute(text("VACUUM INTO :target"), {"target": str(target)})
    db.commit()
    if not target.exists():
        raise RuntimeError(f"VACUUM INTO did not produce file: {target}")

    if exclude_tables:
        from sqlalchemy import create_engine, inspect

        copy_engine = create_engine(f"sqlite:///{target}")
        try:
            # **保留 schema,只 DELETE 数据**(原来用 DROP TABLE 会让 restore
            # 出来的 DB 缺表,server 启动后查到这些表就 500 —— 历史 issue:
            # 0008+ 之后 mcp_call_logs 不在表里,导致 GET /profile/pats 报
            # "no such table"。运维类表本身体积也不大,留 schema 不影响
            # 备份大小。)
            inspector = inspect(copy_engine)
            existing_tables = set(inspector.get_table_names())
            with copy_engine.begin() as conn:
                for tbl in exclude_tables:
                    if tbl not in existing_tables:
                        # 表本来就不存在(老 DB 还没跑过这个 migration)— 跳过
                        continue
                    # 表名是常量白名单,无注入风险
                    table = Table(tbl, MetaData(), autoload_with=conn)
                    conn.execute(delete(table))
            # VACUUM 释放数据占用的空间(SQLite 不会自动收回)
            with copy_engine.connect() as conn:
                conn.execute(text("VACUUM"))
                conn.commit()
            logger.info(
                "vacuum_into: cleared data in %d tables (schema preserved): %s",
                len(exclude_tables), ", ".join(exclude_tables),
            )
        finally:
            copy_engine.dispose()

    size = target.stat().st_size
    logger.info("VACUUM INTO done: %s (%d bytes)", target, size)
