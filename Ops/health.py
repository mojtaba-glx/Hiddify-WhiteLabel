"""Offline health checks which never print or return credential values."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from Database.connection import connect
from Database.migrate import MIGRATIONS_DIR, applied_versions, pending_migrations, verify_checksums
from Gateway.catalog import RuntimeCatalog
from Shared.crypto import FernetTokenCipher
from Shared.settings import load_from_env, load_runtime_from_env


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class HealthReport:
    checks: tuple[HealthCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def run_healthcheck(env_path: Path | str) -> HealthReport:
    env_file = Path(env_path).resolve()
    checks: list[HealthCheck] = []
    if not env_file.is_file():
        return HealthReport((HealthCheck("environment", False, "file missing"),))
    env_mode = stat.S_IMODE(env_file.stat().st_mode)
    checks.append(HealthCheck("environment_permissions", env_mode == 0o600, f"mode={env_mode:04o}"))
    try:
        values = {key: str(value or "") for key, value in dotenv_values(env_file).items()}
        settings = load_from_env(values)
        runtime_settings = load_runtime_from_env(values)
        checks.append(HealthCheck("environment", True, "valid"))
    except Exception as exc:
        checks.append(HealthCheck("environment", False, f"invalid:{type(exc).__name__}"))
        return HealthReport(tuple(checks))

    db_path = Path(settings.database_path)
    if not db_path.is_absolute():
        db_path = env_file.parent / db_path
    if not db_path.is_file():
        checks.append(HealthCheck("database", False, "file missing"))
        return HealthReport(tuple(checks))
    db_mode = stat.S_IMODE(db_path.stat().st_mode)
    checks.append(HealthCheck("database_permissions", db_mode == 0o600, f"mode={db_mode:04o}"))
    try:
        conn = connect(str(db_path))
        try:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
            verify_checksums(conn, MIGRATIONS_DIR)
            expected = {path.stem for path in pending_migrations(MIGRATIONS_DIR)}
            applied = applied_versions(conn)
            catalog = RuntimeCatalog(
                conn,
                cipher=FernetTokenCipher(runtime_settings.token_encryption_key),
                shard_count=1,
                shard_index=0,
            ).load()
        finally:
            conn.close()
        checks.append(HealthCheck("sqlite", quick == "ok", quick))
        checks.append(HealthCheck("foreign_keys", not foreign, f"violations={len(foreign)}"))
        checks.append(HealthCheck("migrations", expected == applied, f"applied={len(applied)}/{len(expected)}"))
        checks.append(HealthCheck("runtime_credentials", not catalog.rejected_bot_ids, f"rejected={len(catalog.rejected_bot_ids)}"))
    except Exception as exc:
        checks.append(HealthCheck("database", False, f"invalid:{type(exc).__name__}"))
    return HealthReport(tuple(checks))
