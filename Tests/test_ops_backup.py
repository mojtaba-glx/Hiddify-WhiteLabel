"""Encrypted backups round-trip DB/env and create a private rollback snapshot."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from Database.migrate import migrate
from Ops.backup import BackupError, create_backup, restore_backup
from Shared.crypto import generate_key

PASSPHRASE = "correct horse battery staple"
MASTER_TOKEN = "990001:FAKE_backup_master_token_abcdefghijklmnopqrstuvwxyz"


def _fixture(tmp_path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    rollback = tmp_path / "rollback"
    for directory in (data, backups, rollback):
        directory.mkdir(mode=0o700)
    database = data / "white.db"
    migrate(str(database))
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE backup_marker (value TEXT)")
    conn.execute("INSERT INTO backup_marker VALUES ('original')")
    conn.commit()
    conn.close()
    env = tmp_path / ".env"
    env.write_text(
        f"MASTER_BOT_TOKEN={MASTER_TOKEN}\n"
        "MASTER_ADMIN_ID=123456\n"
        f"TOKEN_ENCRYPTION_KEY={generate_key()}\n"
        f"DATABASE_PATH={database}\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o600)
    return database, env, backups, rollback


def test_encrypted_backup_round_trip_and_rollback(tmp_path) -> None:
    database, env, backups, rollback = _fixture(tmp_path)
    backup = create_backup(
        database_path=database,
        environment_path=env,
        output_directory=backups,
        passphrase=PASSPHRASE,
        version="0.6.0",
    )
    raw = backup.read_bytes()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert MASTER_TOKEN.encode() not in raw
    assert b"original" not in raw

    conn = sqlite3.connect(database)
    conn.execute("UPDATE backup_marker SET value='changed'")
    conn.commit()
    conn.close()
    env.write_text(env.read_text(encoding="utf-8").replace("123456", "999999"), encoding="utf-8")
    os.chmod(env, 0o600)

    result = restore_backup(
        backup_path=backup,
        database_path=database,
        environment_path=env,
        rollback_root=rollback,
        passphrase=PASSPHRASE,
        services_stopped=True,
    )
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT value FROM backup_marker").fetchone()[0] == "original"
    finally:
        conn.close()
    assert "MASTER_ADMIN_ID=123456" in env.read_text(encoding="utf-8")
    assert stat.S_IMODE(result.rollback_directory.stat().st_mode) == 0o700
    assert (result.rollback_directory / "database.sqlite").is_file()
    assert stat.S_IMODE((result.rollback_directory / "environment.env").stat().st_mode) == 0o600


def test_wrong_passphrase_and_tampering_fail_without_changes(tmp_path) -> None:
    database, env, backups, rollback = _fixture(tmp_path)
    backup = create_backup(
        database_path=database,
        environment_path=env,
        output_directory=backups,
        passphrase=PASSPHRASE,
        version="0.6.0",
    )
    before = database.read_bytes()
    with pytest.raises(BackupError, match="authentication"):
        restore_backup(
            backup_path=backup,
            database_path=database,
            environment_path=env,
            rollback_root=rollback,
            passphrase="wrong passphrase value",
            services_stopped=True,
        )
    assert database.read_bytes() == before
    raw = bytearray(backup.read_bytes())
    raw[-10] ^= 1
    backup.write_bytes(raw)
    os.chmod(backup, 0o600)
    with pytest.raises(BackupError, match="authentication"):
        restore_backup(
            backup_path=backup,
            database_path=database,
            environment_path=env,
            rollback_root=rollback,
            passphrase=PASSPHRASE,
            services_stopped=True,
        )


def test_restore_requires_offline_confirmation_and_private_files(tmp_path) -> None:
    database, env, backups, rollback = _fixture(tmp_path)
    backup = create_backup(
        database_path=database,
        environment_path=env,
        output_directory=backups,
        passphrase=PASSPHRASE,
        version="0.6.0",
    )
    with pytest.raises(BackupError, match="services are stopped"):
        restore_backup(
            backup_path=backup,
            database_path=database,
            environment_path=env,
            rollback_root=rollback,
            passphrase=PASSPHRASE,
        )
    os.chmod(backup, 0o644)
    with pytest.raises(BackupError, match="private permissions"):
        restore_backup(
            backup_path=backup,
            database_path=database,
            environment_path=env,
            rollback_root=rollback,
            passphrase=PASSPHRASE,
            services_stopped=True,
        )


def test_disaster_restore_recreates_missing_database_and_environment(tmp_path) -> None:
    database, env, backups, rollback = _fixture(tmp_path)
    backup = create_backup(
        database_path=database,
        environment_path=env,
        output_directory=backups,
        passphrase=PASSPHRASE,
        version="0.6.0",
    )
    database.unlink()
    env.unlink()
    result = restore_backup(
        backup_path=backup,
        database_path=database,
        environment_path=env,
        rollback_root=rollback,
        passphrase=PASSPHRASE,
        services_stopped=True,
    )
    assert result.database_path.is_file()
    assert result.environment_path == env and env.is_file()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_failed_environment_replace_recovers_previous_files(tmp_path, monkeypatch) -> None:
    database, env, backups, rollback = _fixture(tmp_path)
    backup = create_backup(
        database_path=database,
        environment_path=env,
        output_directory=backups,
        passphrase=PASSPHRASE,
        version="0.6.0",
    )
    conn = sqlite3.connect(database)
    conn.execute("UPDATE backup_marker SET value='current-before-failed-restore'")
    conn.commit()
    conn.close()
    env.write_text(env.read_text(encoding="utf-8").replace("123456", "777777"), encoding="utf-8")
    os.chmod(env, 0o600)
    before_env = env.read_bytes()

    import Ops.backup as backup_module

    real_replace = backup_module.os.replace

    def fail_environment_replace(source, destination):
        if str(destination) == str(env):
            raise OSError("simulated atomic replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(backup_module.os, "replace", fail_environment_replace)
    with pytest.raises(BackupError, match="previous files were recovered"):
        restore_backup(
            backup_path=backup,
            database_path=database,
            environment_path=env,
            rollback_root=rollback,
            passphrase=PASSPHRASE,
            services_stopped=True,
        )
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT value FROM backup_marker").fetchone()[0] == "current-before-failed-restore"
    finally:
        conn.close()
    assert env.read_bytes() == before_env
