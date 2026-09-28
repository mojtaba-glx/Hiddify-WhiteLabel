"""Installer is English-only, syntactically valid and has a safe dry run."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_bash_syntax() -> None:
    result = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_dry_run_does_not_require_root_or_reveal_secrets() -> None:
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "install"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "DRY-RUN" in output
    assert "systemctl" in output
    assert "MASTER_BOT_TOKEN" not in output
    assert "توکن" not in output


def test_restart_dry_run_stops_then_starts_all_units() -> None:
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "restart"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert "systemctl stop hiddify-whitelabel-master.service" in output
    assert "systemctl start hiddify-whitelabel-master.service" in output
    assert output.index("systemctl stop") < output.index("systemctl start")
