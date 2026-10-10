"""Alembic chain invariants required for portable SQLite/PostgreSQL upgrades."""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_revision_ids_fit_alembic_version_column() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    long_revisions = [
        (revision.revision, len(revision.revision))
        for revision in script.walk_revisions()
        if len(revision.revision) > 32
    ]
    assert long_revisions == [], (
        "Alembic's default version_num column is VARCHAR(32); "
        f"revision IDs exceed it: {long_revisions}"
    )


def test_revision_chain_has_single_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert len(script.get_heads()) == 1
