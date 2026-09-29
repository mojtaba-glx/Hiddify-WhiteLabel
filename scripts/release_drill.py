#!/usr/bin/env python3
"""Run the offline release-readiness drill for Hiddify WhiteLabel."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Ops.release_drill import build_offline_plan, plan_as_dict, run_offline_drill


def _report_payload(report):
    return {
        "ok": report.ok,
        "results": [
            {
                "name": result.name,
                "ok": result.ok,
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
                "output_tail": result.output_tail,
            }
            for result in report.results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the non-network release gate before the operator performs "
            "the real disposable-panel staging drill."
        )
    )
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help=(
            "run the complete pytest suite instead of the critical "
            "regression subset"
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="print the deterministic offline plan without executing it",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    args = parser.parse_args()

    if args.plan_only:
        plan = build_offline_plan(ROOT, full_suite=args.full_suite)
        payload = {
            "mode": "offline",
            "network": False,
            "steps": plan_as_dict(plan),
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            for index, step in enumerate(plan, 1):
                print(f"{index}. {step.name}: {shlex.join(step.argv)}")
        return 0

    report = run_offline_drill(ROOT, full_suite=args.full_suite)
    if args.json:
        print(json.dumps(_report_payload(report), sort_keys=True))
    else:
        for result in report.results:
            status = "PASS" if result.ok else "FAIL"
            print(
                f"{status}: {result.name} "
                f"({result.duration_seconds:.3f}s)"
            )
            if not result.ok and result.output_tail:
                print(result.output_tail)
        print(
            "READY FOR LIVE STAGING"
            if report.ok
            else "RELEASE DRILL FAILED"
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
