#!/usr/bin/env python3
"""Restore an encrypted backup after services have been stopped."""

from __future__ import annotations

import argparse
import getpass
import stat
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Ops.backup import BackupError, restore_backup
from Shared.settings import load_from_env


def _passphrase(path: str | None) -> str:
    if not path:
        return getpass.getpass("Backup passphrase (hidden): ")
    source = Path(path).resolve()
    if not source.is_file() or stat.S_IMODE(source.stat().st_mode) != 0o600:
        raise BackupError("passphrase file must exist and have mode 0600")
    return source.read_text(encoding="utf-8").rstrip("\r\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore encrypted WhiteLabel backup")
    parser.add_argument("backup_file")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--database", help="database destination if .env is missing or unusable")
    parser.add_argument("--rollback-dir", default=str(ROOT / "runtime" / "rollback"))
    parser.add_argument("--passphrase-file")
    parser.add_argument("--yes", action="store_true", help="confirm destructive restore")
    args = parser.parse_args()
    if not args.yes:
        print("Restore requires --yes after all WhiteLabel services are stopped.", file=sys.stderr)
        return 2
    try:
        env_path = Path(args.env_file).resolve()
        if args.database:
            database = Path(args.database).resolve()
        elif env_path.is_file():
            try:
                values = {key: str(value or "") for key, value in dotenv_values(env_path).items()}
                settings = load_from_env(values)
                database = Path(settings.database_path)
                if not database.is_absolute():
                    database = env_path.parent / database
            except Exception:
                database = ROOT / "data" / "whitelabel.db"
        else:
            database = ROOT / "data" / "whitelabel.db"
        passphrase = _passphrase(args.passphrase_file)
        try:
            result = restore_backup(
                backup_path=args.backup_file,
                database_path=database,
                environment_path=env_path,
                rollback_root=args.rollback_dir,
                passphrase=passphrase,
                services_stopped=True,
            )
        finally:
            del passphrase
        print(f"Restore completed. Rollback snapshot: {result.rollback_directory}")
        return 0
    except Exception as exc:
        print(f"Restore failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
