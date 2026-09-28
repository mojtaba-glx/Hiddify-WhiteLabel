"""Installer and one-line bootstrap are syntactically valid and safe to dry-run."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_bash_syntax() -> None:
    for path in (ROOT / "install.sh", ROOT / "bootstrap.sh"):
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path}: {result.stderr}"


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


def test_update_dry_run_does_not_touch_git_or_require_root() -> None:
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "update"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "DRY-RUN" in output
    assert "fetch" in output
    assert "migrate" in output
    assert "restart" in output


def test_full_uninstall_dry_run_is_non_destructive() -> None:
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--dry-run", "uninstall-full"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "DRY-RUN" in output
    assert "project files" in output
    assert ROOT.exists()


def test_version_command_matches_version_file() -> None:
    result = subprocess.run(
        [str(ROOT / "install.sh"), "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (ROOT / "VERSION").read_text().strip()


def test_installer_contains_manager_and_safe_update_controls() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "/usr/local/bin/whitelabel" in content
    assert "git -C" in content
    assert "pytest -q" in content
    assert "tracked project files have local changes" in content
    assert "Type DELETE ALL" in content


def test_bootstrap_uses_dedicated_service_account_and_opt_path() -> None:
    content = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "/opt/hiddify-whitelabel" in content
    assert "whitelabel" in content
    assert "useradd --system" in content
    assert "--user-group" in content
    assert "git clone" in content
    assert "install.sh\" update" in content or "install.sh\" install" in content


def test_bootstrap_dry_run_is_safe_and_describes_full_install() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "DRY-RUN" in output
    assert "apt prerequisites" in output
    assert "dedicated service account" in output
    assert "git" in output.lower()
    assert "install.sh install" in output
    assert "/usr/local/bin/whitelabel" in output
