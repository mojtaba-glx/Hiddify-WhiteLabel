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
    assert "update_snapshot.py" in content
    assert "ROLLBACK OK" in content
    assert "automatic database/environment rollback failed" in content


def test_bootstrap_uses_dedicated_service_account_and_opt_path() -> None:
    content = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "/opt/hiddify-whitelabel" in content
    assert "whitelabel" in content
    assert "useradd" in content
    assert "--system" in content
    assert "--user-group" in content
    assert "git clone" in content
    assert "SERVICE_GROUP=" in content
    assert 'mkdir -p "$(dirname "$INSTALL_DIR")"' in content
    assert 'install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$INSTALL_DIR"' in content
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


def test_shard_change_has_env_and_systemd_rollback() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "env-before-shards-$$" in content
    assert "restoring previous configuration" in content
    assert "rollback_units" in content


def test_token_change_uses_pid_scoped_env_backup() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "env-before-token-$$" in content


def test_settings_include_timezone_and_nonsecret_summary() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "Show current non-secret settings" in content
    assert "Change display timezone" in content
    assert "change_timezone()" in content
    assert "MasterBot token: $token_state" in content
    summary = content.split("show_nonsecret_settings() {", 1)[1].split("logs_menu() {", 1)[0]
    assert "sed -n 's/^MASTER_BOT_TOKEN=//p'" not in summary
    assert 'echo "$master_token"' not in summary


def test_admin_change_has_env_rollback() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'mktemp "$ROOT_DIR/runtime/env-before-admin.XXXXXX"' in content
    assert "failed after admin ID change; restoring previous .env" in content


def test_update_restores_snapshot_before_old_source_reset() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    update = content.split("update_action() {", 1)[1].split("backup_action() {", 1)[0]
    restore = 'update_snapshot.py" restore "$snapshot_dir"'
    reset = 'reset --hard "$old_sha"'
    # The first reset is the pre-downtime test failure path. In the post-snapshot
    # rollback block, snapshot restore must appear before the later source reset.
    error_block = update.split("update failed after snapshot", 1)[1]
    assert error_block.index(restore) < error_block.index(reset)
