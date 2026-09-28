#!/usr/bin/env python3
"""Apply pending migrations to a local SQLite file (offline, no secrets)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Database.migrate import migrate
from Shared.settings import load_database_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply White-Label migrations")
    parser.add_argument("--db", help="SQLite file path")
    parser.add_argument("--env-file", help="Resolve DATABASE_PATH from this environment file")
    args = parser.parse_args()
    if args.db and args.env_file:
        parser.error("use either --db or --env-file")
    if args.env_file:
        env_path = Path(args.env_file).resolve()
        values = {key: str(value or "") for key, value in dotenv_values(env_path).items()}
        database_path = Path(load_database_path(values))
        if not database_path.is_absolute():
            database_path = env_path.parent / database_path
    else:
        database_path = Path(args.db or "data/whitelabel.db")
    applied = migrate(str(database_path))
    print(f"applied: {applied if applied else 'nothing pending'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
