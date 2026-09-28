#!/usr/bin/env python3
"""Create an authenticated encrypted backup; passphrase is never an argument."""

from __future__ import annotations

import argparse
import getpass
import stat
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Ops.backup import BackupError, create_backup
from Shared.settings import load_from_env


def _read_passphrase(path: str | None, *, confirm: bool) -> str:
    if path:
        source = Path(path).resolve()
        if not source.is_file() or stat.S_IMODE(source.stat().st_mode) != 0o600:
            raise BackupError("passphrase file must exist and have mode 0600")
        value = source.read_text(encoding="utf-8").rstrip("\r\n")
    else:
        value = getpass.getpass("Backup passphrase (hidden): ")
        if confirm and value != getpass.getpass("Repeat passphrase (hidden): "):
            raise BackupError("passphrases do not match")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Create encrypted WhiteLabel backup")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--output-dir", default=str(ROOT / "backups"))
    parser.add_argument("--passphrase-file")
    args = parser.parse_args()
    try:
        env_path = Path(args.env_file).resolve()
        values = {key: str(value or "") for key, value in dotenv_values(env_path).items()}
        settings = load_from_env(values)
        database = Path(settings.database_path)
        if not database.is_absolute():
            database = env_path.parent / database
        passphrase = _read_passphrase(args.passphrase_file, confirm=True)
        try:
            output = create_backup(
                database_path=database,
                environment_path=env_path,
                output_directory=args.output_dir,
                passphrase=passphrase,
                version=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            )
        finally:
            del passphrase
        print(f"Backup created: {output}")
        return 0
    except Exception as exc:
        print(f"Backup failed ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
