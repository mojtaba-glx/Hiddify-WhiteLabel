"""Deterministic staged-release gate for Hiddify WhiteLabel.

The automated portion is intentionally offline. It exercises the critical
provider contracts, subscription lifecycle, tenant commerce and operational
rollback paths without contacting Telegram or any real panel. Real panel
validation is an operator-run staging step documented separately.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class DrillStep:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 900


@dataclass(frozen=True)
class DrillResult:
    name: str
    ok: bool
    returncode: int
    duration_seconds: float
    output_tail: str


@dataclass(frozen=True)
class DrillReport:
    results: tuple[DrillResult, ...]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(item.ok for item in self.results)


def _required_paths(root: Path) -> tuple[Path, ...]:
    return (
        root / "requirements.txt",
        root / "install.sh",
        root / "bootstrap.sh",
        root / "scripts" / "migrate.py",
        root / "Tests" / "test_hiddify_live.py",
        root / "Tests" / "test_xui_live.py",
        root / "Tests" / "test_xnet_live.py",
        root / "Tests" / "test_subscription_lifecycle.py",
        root / "Tests" / "test_multinode_smart_subscription.py",
        root / "Tests" / "test_reminder_global_enforcer.py",
        root / "Tests" / "test_tenant_reports_admin_user.py",
        root / "Tests" / "test_tenant_sales_growth.py",
        root / "Tests" / "test_customer_purchase_e2e.py",
        root / "Tests" / "test_update_snapshot.py",
        root / "Tests" / "test_ops_health.py",
        root / "Tests" / "test_install_script.py",
    )


def validate_repository(root: Path | str) -> Path:
    repo = Path(root).resolve()
    missing = [
        path.relative_to(repo).as_posix()
        for path in _required_paths(repo)
        if not path.is_file()
    ]
    if missing:
        raise ValueError("release drill repository is incomplete: " + ", ".join(missing))
    return repo


def build_offline_plan(
    root: Path | str, *, full_suite: bool = False
) -> tuple[DrillStep, ...]:
    repo = validate_repository(root)
    py = sys.executable
    if full_suite:
        pytest_steps = (
            DrillStep("full-offline-suite", (py, "-m", "pytest", "-q"), 1800),
        )
    else:
        pytest_steps = (
            DrillStep(
                "provider-contracts",
                (
                    py,
                    "-m",
                    "pytest",
                    "-q",
                    "Tests/test_hiddify_live.py",
                    "Tests/test_xui_live.py",
                    "Tests/test_xnet_live.py",
                ),
            ),
            DrillStep(
                "subscription-lifecycle",
                (
                    py,
                    "-m",
                    "pytest",
                    "-q",
                    "Tests/test_subscription_lifecycle.py",
                    "Tests/test_multinode_smart_subscription.py",
                    "Tests/test_reminder_global_enforcer.py",
                ),
            ),
            DrillStep(
                "tenant-commerce-and-admin",
                (
                    py,
                    "-m",
                    "pytest",
                    "-q",
                    "Tests/test_tenant_reports_admin_user.py",
                    "Tests/test_tenant_sales_growth.py",
                    "Tests/test_customer_purchase_e2e.py",
                ),
            ),
            DrillStep(
                "operations-and-rollback",
                (
                    py,
                    "-m",
                    "pytest",
                    "-q",
                    "Tests/test_update_snapshot.py",
                    "Tests/test_ops_health.py",
                    "Tests/test_install_script.py",
                ),
            ),
        )

    return pytest_steps + (
        DrillStep(
            "install-shell-syntax",
            ("bash", "-n", str(repo / "install.sh")),
            60,
        ),
        DrillStep(
            "bootstrap-shell-syntax",
            ("bash", "-n", str(repo / "bootstrap.sh")),
            60,
        ),
        DrillStep(
            "fresh-migration-smoke",
            (py, str(repo / "scripts" / "migrate.py"), "--db", "{TEMP_DB}"),
            180,
        ),
    )


def _tail(text: str, *, lines: int = 40) -> str:
    parts = str(text or "").splitlines()
    return "\n".join(parts[-max(1, int(lines)):])


def run_offline_drill(
    root: Path | str,
    *,
    full_suite: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    extra_env: dict[str, str] | None = None,
) -> DrillReport:
    repo = validate_repository(root)
    plan = build_offline_plan(repo, full_suite=full_suite)
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
    results: list[DrillResult] = []

    with tempfile.TemporaryDirectory(
        prefix="whitelabel-release-drill-"
    ) as temp_dir:
        temp_db = str(Path(temp_dir) / "fresh.db")
        for step in plan:
            argv = tuple(
                temp_db if part == "{TEMP_DB}" else part
                for part in step.argv
            )
            started = time.monotonic()
            try:
                completed = runner(
                    argv,
                    cwd=str(repo),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=step.timeout_seconds,
                    check=False,
                )
                returncode = int(completed.returncode)
                output = str(completed.stdout or "")
            except subprocess.TimeoutExpired as exc:
                returncode = 124
                output = str(exc.stdout or "") + "\nTIMEOUT"
            duration = max(0.0, time.monotonic() - started)
            result = DrillResult(
                name=step.name,
                ok=returncode == 0,
                returncode=returncode,
                duration_seconds=round(duration, 3),
                output_tail=_tail(output),
            )
            results.append(result)
            if not result.ok:
                break

    return DrillReport(tuple(results))


def plan_as_dict(
    steps: Sequence[DrillStep],
) -> list[dict[str, object]]:
    return [
        {
            "name": step.name,
            "argv": list(step.argv),
            "timeout_seconds": step.timeout_seconds,
        }
        for step in steps
    ]
