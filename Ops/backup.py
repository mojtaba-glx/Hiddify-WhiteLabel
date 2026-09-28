"""Encrypted, integrity-checked SQLite + environment backup and restore."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import dotenv_values

from Database.connection import connect, enforce_file_modes
from Database.migrate import MIGRATIONS_DIR, migrate, verify_checksums
from Shared.settings import load_from_env

MAGIC = b"WLBAK1\n"
PBKDF2_ITERATIONS = 600_000
MIN_PASSPHRASE_LENGTH = 12
ALLOWED_MEMBERS = frozenset({"manifest.json", "database.sqlite", "environment.env"})


class BackupError(RuntimeError):
    """Backup input, encryption, integrity, or restore validation failed."""


@dataclass(frozen=True)
class RestoreResult:
    database_path: Path
    environment_path: Path | None
    rollback_directory: Path


def _private_directory(path: Path) -> Path:
    if path.exists():
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise BackupError("backup directory must be private (0700)")
    else:
        path.mkdir(parents=True, mode=0o700)
    return path


def _passphrase(value: str) -> bytes:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) < MIN_PASSPHRASE_LENGTH:
        raise BackupError("backup passphrase must contain at least 12 bytes")
    return encoded


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    ).derive(_passphrase(passphrase))
    return Fernet(base64.urlsafe_b64encode(key))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sqlite_snapshot(
    source_path: Path, directory: Path, *, filename: str = "snapshot.sqlite"
) -> bytes:
    snapshot = directory / filename
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(snapshot)
    try:
        source.backup(target)
        target.commit()
        if str(target.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise BackupError("SQLite snapshot integrity check failed")
    finally:
        target.close()
        source.close()
    return snapshot.read_bytes()


def _archive(database: bytes, environment: bytes | None, version: str) -> bytes:
    files = {"database.sqlite": _sha256(database)}
    if environment is not None:
        files["environment.env"] = _sha256(environment)
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": str(version).strip(),
        "files": files,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        archive.writestr("database.sqlite", database)
        if environment is not None:
            archive.writestr("environment.env", environment)
    return output.getvalue()


def create_backup(
    *,
    database_path: Path | str,
    environment_path: Path | str | None,
    output_directory: Path | str,
    passphrase: str,
    version: str,
) -> Path:
    database = Path(database_path).resolve()
    if not database.is_file():
        raise BackupError("database file does not exist")
    if stat.S_IMODE(database.stat().st_mode) != 0o600:
        raise BackupError("database file must have mode 0600")
    environment = Path(environment_path).resolve() if environment_path else None
    if environment is not None:
        if not environment.is_file():
            raise BackupError("environment file does not exist")
        if stat.S_IMODE(environment.stat().st_mode) != 0o600:
            raise BackupError("environment file must have mode 0600")
    output_dir = _private_directory(Path(output_directory).resolve())
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output_dir) as raw_temp:
        temp_dir = Path(raw_temp)
        os.chmod(temp_dir, 0o700)
        database_bytes = _sqlite_snapshot(database, temp_dir)
        environment_bytes = environment.read_bytes() if environment else None
        payload = _archive(database_bytes, environment_bytes, version)
    salt = secrets.token_bytes(16)
    encrypted = _fernet(passphrase, salt).encrypt(payload)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_dir / f"whitelabel-{stamp}-{secrets.token_hex(3)}.wlbak"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(MAGIC + salt + encrypted)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def _decrypt_archive(path: Path, passphrase: str) -> dict[str, bytes]:
    raw = path.read_bytes()
    if len(raw) < len(MAGIC) + 17 or not raw.startswith(MAGIC):
        raise BackupError("invalid backup format")
    salt_start = len(MAGIC)
    salt = raw[salt_start : salt_start + 16]
    try:
        payload = _fernet(passphrase, salt).decrypt(raw[salt_start + 16 :])
    except InvalidToken:
        raise BackupError("backup authentication failed") from None
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or not set(names).issubset(ALLOWED_MEMBERS):
                raise BackupError("backup contains unexpected members")
            if "manifest.json" not in names or "database.sqlite" not in names:
                raise BackupError("backup is incomplete")
            content = {name: archive.read(name) for name in names}
        manifest = json.loads(content["manifest.json"].decode("utf-8"))
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise BackupError("backup manifest is invalid")
        expected_files = set(manifest["files"])
        actual_files = set(content) - {"manifest.json"}
        if expected_files != actual_files:
            raise BackupError("backup manifest does not match payload")
        for name in actual_files:
            if not secrets.compare_digest(str(manifest["files"][name]), _sha256(content[name])):
                raise BackupError("backup payload checksum mismatch")
        return content
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup payload is invalid") from None


def _validate_environment(raw: bytes, env_path: Path, database_path: Path) -> None:
    try:
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise ValueError
        values = {key: str(value or "") for key, value in dotenv_values(stream=io.StringIO(text)).items()}
        settings = load_from_env(values)
        configured = Path(settings.database_path)
        if not configured.is_absolute():
            configured = env_path.parent / configured
        if configured.resolve() != database_path.resolve():
            raise ValueError
    except Exception:
        raise BackupError("backup environment is invalid") from None


def _validate_staged_database(path: Path, migrations_dir: Path) -> None:
    connection = connect(str(path))
    try:
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise BackupError("restored SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("restored SQLite foreign keys are invalid")
        verify_checksums(connection, migrations_dir)
    finally:
        connection.close()
    migrate(str(path), migrations_dir)


def restore_backup(
    *,
    backup_path: Path | str,
    database_path: Path | str,
    environment_path: Path | str | None,
    rollback_root: Path | str,
    passphrase: str,
    migrations_dir: Path = MIGRATIONS_DIR,
    services_stopped: bool = False,
) -> RestoreResult:
    if not services_stopped:
        raise BackupError("restore requires explicit confirmation that services are stopped")
    backup = Path(backup_path).resolve()
    if not backup.is_file() or stat.S_IMODE(backup.stat().st_mode) & 0o077:
        raise BackupError("backup file must exist and have private permissions")
    database = Path(database_path).resolve()
    environment = Path(environment_path).resolve() if environment_path else None
    content = _decrypt_archive(backup, passphrase)
    if "environment.env" in content:
        if environment is None:
            raise BackupError("backup includes environment but no restore path was provided")
        _validate_environment(content["environment.env"], environment, database)

    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(database.parent.stat().st_mode) & 0o077:
        raise BackupError("database directory must be private (0700)")
    rollback_base = _private_directory(Path(rollback_root).resolve())
    rollback = rollback_base / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rollback = rollback.with_name(f"{rollback.name}-{secrets.token_hex(3)}")
    rollback.mkdir(mode=0o700)

    with tempfile.TemporaryDirectory(prefix=".restore-", dir=database.parent) as raw_temp:
        stage_dir = Path(raw_temp)
        os.chmod(stage_dir, 0o700)
        staged_db = stage_dir / "database.sqlite"
        staged_db.write_bytes(content["database.sqlite"])
        os.chmod(staged_db, 0o600)
        _validate_staged_database(staged_db, migrations_dir)
        staged_env = None
        if "environment.env" in content and environment is not None:
            staged_env = stage_dir / "environment.env"
            staged_env.write_bytes(content["environment.env"])
            os.chmod(staged_env, 0o600)

        database_existed = database.exists()
        environment_existed = environment is not None and environment.exists()
        if database_existed:
            _sqlite_snapshot(database, rollback, filename="database.sqlite")
            os.chmod(rollback / "database.sqlite", 0o600)
        if environment_existed and environment is not None:
            shutil.copy2(environment, rollback / "environment.env")
            os.chmod(rollback / "environment.env", 0o600)
        try:
            # These sidecars belong to the replaced database. The caller has
            # explicitly confirmed that all service processes are stopped.
            for suffix in ("-wal", "-shm", "-journal"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)
            os.replace(staged_db, database)
            os.chmod(database, 0o600)
            if staged_env is not None and environment is not None:
                os.replace(staged_env, environment)
                os.chmod(environment, 0o600)
        except Exception:
            old_db = rollback / "database.sqlite"
            old_env = rollback / "environment.env"
            if old_db.exists():
                shutil.copy2(old_db, database)
                enforce_file_modes(str(database))
            elif not database_existed:
                database.unlink(missing_ok=True)
            if environment is not None and old_env.exists():
                shutil.copy2(old_env, environment)
                os.chmod(environment, 0o600)
            elif environment is not None and not environment_existed:
                environment.unlink(missing_ok=True)
            raise BackupError("restore failed; previous files were recovered") from None
    return RestoreResult(database, environment if "environment.env" in content else None, rollback)
