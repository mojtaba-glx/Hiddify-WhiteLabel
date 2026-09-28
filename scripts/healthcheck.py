#!/usr/bin/env python3
"""Run offline configuration/database health checks without revealing secrets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Ops.health import run_healthcheck


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Hiddify WhiteLabel health")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_healthcheck(args.env_file)
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "checks": [check.__dict__ for check in report.checks],
        }, sort_keys=True))
    else:
        for check in report.checks:
            print(f"{'OK' if check.ok else 'ERROR'}: {check.name} ({check.detail})")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
