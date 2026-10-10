"""SQLite/PostgreSQL compatibility tests for dialect-aware database access."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import User, UserAccountProjection
from src.projection import _dialect_insert, _upsert

POSTGRES_URL = os.environ.get("DATABASE_URL", "")


def test_projection_insert_constructors_follow_dialect() -> None:
    assert _dialect_insert("sqlite") is sqlite_insert
    assert _dialect_insert("postgresql") is postgresql_insert

    values = {
        "user_id": "u1",
        "sync_id": "acc-1",
        "name": "Cash",
        "account_type": "cash",
        "currency": "CNY",
        "initial_balance": 0.0,
        "source_change_id": 1,
    }
    sqlite_stmt = _dialect_insert("sqlite")(UserAccountProjection).values(**values)
    postgres_stmt = _dialect_insert("postgresql")(UserAccountProjection).values(**values)

    # These are distinct dialect constructs: the PG object must not be reused
    # from the SQLite dialect, which is what caused production compile errors.
    assert type(sqlite_stmt) is not type(postgres_stmt)


def _account_values(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "sync_id": "acc-dialect",
        "name": "Cash",
        "account_type": "cash",
        "currency": "CNY",
        "initial_balance": 0.0,
        "hidden": False,
        "source_change_id": 1,
    }


def test_upsert_sqlite() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        with Session(bind=engine) as db:
            user_id = str(uuid.uuid4())
            db.add(
                User(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    password_hash="test",
                    is_admin=False,
                    is_enabled=True,
                )
            )
            db.flush()

            values = _account_values(user_id)
            _upsert(db, UserAccountProjection, ("user_id", "sync_id"), values)
            values["name"] = "Cash renamed"
            _upsert(db, UserAccountProjection, ("user_id", "sync_id"), values)

            account = db.execute(
                UserAccountProjection.__table__.select().where(
                    UserAccountProjection.user_id == user_id,
                    UserAccountProjection.sync_id == "acc-dialect",
                )
            ).mappings().one()
            assert account["name"] == "Cash renamed"
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_URL.startswith("postgresql+psycopg://"),
    reason="requires PostgreSQL DATABASE_URL",
)
def test_upsert_postgres_transaction() -> None:
    from src.database import SessionLocal

    with SessionLocal() as db:
        user_id = str(uuid.uuid4())
        db.add(
            User(
                id=user_id,
                email=f"{user_id}@example.com",
                password_hash="test",
                is_admin=False,
                is_enabled=True,
            )
        )
        db.flush()

        values = _account_values(user_id)
        _upsert(db, UserAccountProjection, ("user_id", "sync_id"), values)
        values["name"] = "Cash renamed"
        _upsert(db, UserAccountProjection, ("user_id", "sync_id"), values)

        account = db.execute(
            UserAccountProjection.__table__.select().where(
                UserAccountProjection.user_id == user_id,
                UserAccountProjection.sync_id == "acc-dialect",
            )
        ).mappings().one()
        assert account["name"] == "Cash renamed"

        # The whole smoke transaction is rolled back to keep CI databases clean.
        db.rollback()


def test_pg_dump_uses_custom_format_and_hidden_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.backup.db_snapshot import pg_dump

    target = tmp_path / "db.dump"
    calls: list[list[str]] = []
    environments: list[dict[str, str]] = []

    class FakeURL:
        host = "postgres.example"
        port = 5432
        username = "beecount"
        password = "secret"
        database = "beecount"

    class FakeEngine:
        url = FakeURL()

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], *, env: dict[str, str], **_kwargs):
        calls.append(command)
        environments.append(env)
        target.write_bytes(b"PGDMP")
        return FakeResult()

    monkeypatch.setattr("src.services.backup.db_snapshot.subprocess.run", fake_run)
    result = pg_dump(FakeEngine(), target)

    assert result == target
    command = calls[0]
    assert command[:2] == ["pg_dump", "--format=custom"]
    assert "--dbname" in command
    assert "secret" not in " ".join(command)
    assert environments[0]["PGPASSWORD"] == "secret"
