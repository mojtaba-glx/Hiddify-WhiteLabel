#!/usr/bin/env python3
"""Atomically update one allow-listed .env value without printing secrets."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path

ALLOWED_KEYS = {
    "MASTER_BOT_TOKEN",
    "MASTER_ADMIN_ID",
    "RUNTIME_SHARD_COUNT",
    "DISPLAY_TIMEZONE",
}


def validate(key: str, value: str) -> str:
    clean = str(value or "").strip()
    if key == "MASTER_BOT_TOKEN":
        if ":" not in clean or any(ch.isspace() for ch in clean):
            raise ValueError("invalid MasterBot token")
    elif key == "MASTER_ADMIN_ID":
        if not clean.isdigit() or int(clean) <= 0:
            raise ValueError("admin id must be a positive integer")
    elif key == "RUNTIME_SHARD_COUNT":
        if not clean.isdigit() or not 1 <= int(clean) <= 64:
            raise ValueError("runtime shard count must be between 1 and 64")
    elif key == "DISPLAY_TIMEZONE":
        if not clean or len(clean) > 100 or any(ch in clean for ch in "\r\n\x00"):
            raise ValueError("invalid timezone")
    else:
        raise ValueError("environment key is not editable")
    return clean


def update_env(path: Path, key: str, value: str) -> None:
    if key not in ALLOWED_KEYS:
        raise ValueError("environment key is not editable")
    clean = validate(key, value)
    if not path.is_file():
        raise FileNotFoundError(path)

    original = path.read_text(encoding="utf-8").splitlines()
    rendered: list[str] = []
    replaced = False
    prefix = f"{key}="
    for line in original:
        if line.startswith(prefix):
            if not replaced:
                rendered.append(prefix + clean)
                replaced = True
            continue
        rendered.append(line)
    if not replaced:
        rendered.append(prefix + clean)

    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temp = Path(handle.name)
        handle.write("\n".join(rendered).rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, mode or 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--key", required=True, choices=sorted(ALLOWED_KEYS))
    args = parser.parse_args()
    value = os.environ.get("WL_ENV_VALUE")
    if value is None:
        raise SystemExit("WL_ENV_VALUE is required")
    update_env(Path(args.env_file).resolve(), args.key, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
