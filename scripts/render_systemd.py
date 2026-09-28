#!/usr/bin/env python3
"""Render systemd units into a directory; does not call systemctl."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Ops.systemd import write_units


def main() -> int:
    parser = argparse.ArgumentParser(description="Render WhiteLabel systemd units")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("--service-user", required=True)
    args = parser.parse_args()
    paths = write_units(
        args.output_dir,
        project_root=args.project_root,
        service_user=args.service_user,
    )
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
