#!/usr/bin/env python3
"""Private pre-update snapshot/restore for local rollback.

This is intentionally not an export backup. It lives under the private runtime
directory and exists only so a failed application update can restore the exact
pre-migration SQLite database and .env without asking for a passphrase.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

from dotenv import dotenv_values


def _database_path(env_file: Path) -> Path:
    values = {key: str(value or "") for key, value in dotenv_values(env_file).items()}
    raw = values.get("DATABASE_PATH", "").strip() or "data/whitelabel.db"
    path = Path(raw)
    return path if path.is_absolute() else env_file.parent / path


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    os.chmod(path, 0o700)


def create_snapshot(env_file: Path, output_root: Path, source_sha: str) -> Path:
    env_file = env_file.resolve()
    if not env_file.is_file():
        raise FileNotFoundError("environment file is missing")
    database = _database_path(env_file).resolve()
    if not database.is_file():
        raise FileNotFoundError("database file is missing")

    output_root.mkdir(parents=True, exist_ok=True)
    os.chmod(output_root, 0o700)
    target = Path(tempfile.mkdtemp(prefix="update-", dir=output_root))
    os.chmod(target, 0o700)

    env_copy = target / ".env"
    shutil.copy2(env_file, env_copy)
    os.chmod(env_copy, 0o600)

    db_copy = target / "database.sqlite3"
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    destination = sqlite3.connect(db_copy)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(db_copy, 0o600)

    metadata = {
        "source_sha": str(source_sha),
        "database_path": str(database),
        "environment_path": str(env_file),
    }
    meta = target / "metadata.json"
    meta.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    os.chmod(meta, 0o600)
    return target


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.restore-", dir=destination.parent)
    os.close(fd)
    temp = Path(name)
    try:
        shutil.copyfile(source, temp)
        os.chmod(temp, mode)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def restore_snapshot(snapshot: Path) -> None:
    snapshot = snapshot.resolve()
    meta = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    env_target = Path(meta["environment_path"]).resolve()
    db_target = Path(meta["database_path"]).resolve()
    env_source = snapshot / ".env"
    db_source = snapshot / "database.sqlite3"
    if not env_source.is_file() or not db_source.is_file():
        raise FileNotFoundError("update snapshot is incomplete")

    # Services must already be stopped by the caller. Replace DB first, then env.
    _atomic_copy(db_source, db_target, 0o600)
    _atomic_copy(env_source, env_target, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)

    create = sub.add_parser("create")
    create.add_argument("--env-file", required=True)
    create.add_argument("--output-root", required=True)
    create.add_argument("--source-sha", required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("snapshot")

    args = parser.parse_args()
    if args.action == "create":
        path = create_snapshot(
            Path(args.env_file), Path(args.output_root), str(args.source_sha)
        )
        print(path)
        return 0
    restore_snapshot(Path(args.snapshot))
    print("Update snapshot restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
