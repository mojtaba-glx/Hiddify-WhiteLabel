"""Private update snapshot rollback tests."""

from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "update_snapshot.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_update_snapshot_restores_database_and_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data = project / "data"
    data.mkdir()
    env_file = project / ".env"
    env_file.write_text(
        "MASTER_BOT_TOKEN=1:test\n"
        "MASTER_ADMIN_ID=100\n"
        "TOKEN_ENCRYPTION_KEY=not-used-here\n"
        "DATABASE_PATH=data/whitelabel.db\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    db = data / "whitelabel.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE state (value TEXT NOT NULL)")
    conn.execute("INSERT INTO state(value) VALUES ('before')")
    conn.commit()
    conn.close()
    db.chmod(0o600)

    root = project / "runtime" / "update-rollback"
    created = _run(
        "create",
        "--env-file",
        str(env_file),
        "--output-root",
        str(root),
        "--source-sha",
        "abc123",
    )
    assert created.returncode == 0, created.stderr
    snapshot = Path(created.stdout.strip())
    assert snapshot.is_dir()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    assert stat.S_IMODE((snapshot / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((snapshot / "database.sqlite3").stat().st_mode) == 0o600
    metadata = json.loads((snapshot / "metadata.json").read_text())
    assert metadata["source_sha"] == "abc123"

    env_file.write_text(env_file.read_text().replace("MASTER_ADMIN_ID=100", "MASTER_ADMIN_ID=999"))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE state SET value = 'after'")
    conn.commit()
    conn.close()
    Path(str(db) + "-wal").write_bytes(b"stale-wal")
    Path(str(db) + "-shm").write_bytes(b"stale-shm")

    restored = _run("restore", str(snapshot))
    assert restored.returncode == 0, restored.stderr
    assert "MASTER_ADMIN_ID=100" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()

    conn = sqlite3.connect(db)
    try:
        value = conn.execute("SELECT value FROM state").fetchone()[0]
    finally:
        conn.close()
    assert value == "before"


def test_snapshot_refuses_missing_database(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_PATH=data/missing.db\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    result = _run(
        "create",
        "--env-file",
        str(env_file),
        "--output-root",
        str(tmp_path / "rollback"),
        "--source-sha",
        "abc",
    )
    assert result.returncode != 0
