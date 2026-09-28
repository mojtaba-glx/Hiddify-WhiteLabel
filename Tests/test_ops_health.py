"""Health output reports status without exposing environment values."""

from __future__ import annotations

import os

from Database.migrate import migrate
from Ops.health import run_healthcheck
from Shared.crypto import generate_key


def _write_env(path, database, token="990001:FAKE_health_token_abcdefghijklmnopqrstuvwxyz"):
    path.write_text(
        "\n".join(
            [
                f"MASTER_BOT_TOKEN={token}",
                "MASTER_ADMIN_ID=123456",
                f"TOKEN_ENCRYPTION_KEY={generate_key()}",
                f"DATABASE_PATH={database}",
                "RUNTIME_SHARD_COUNT=4",
                "RUNTIME_SHARD_INDEX=0",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def test_healthy_fresh_database(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    database = data / "white.db"
    migrate(str(database))
    env = tmp_path / ".env"
    _write_env(env, database)
    report = run_healthcheck(env)
    assert report.ok
    assert {check.name for check in report.checks} >= {
        "environment", "sqlite", "foreign_keys", "migrations", "runtime_credentials"
    }
    rendered = repr(report)
    assert "FAKE_health_token" not in rendered


def test_health_rejects_public_env_and_missing_database(tmp_path) -> None:
    env = tmp_path / ".env"
    _write_env(env, tmp_path / "missing" / "db.sqlite")
    os.chmod(env, 0o644)
    report = run_healthcheck(env)
    assert not report.ok
    values = {check.name: check.ok for check in report.checks}
    assert not values["environment_permissions"]
    assert not values["database"]
