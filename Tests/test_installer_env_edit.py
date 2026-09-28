"""Safe installer environment editing tests."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(env_file: Path, key: str, value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "env_edit.py"),
            "--env-file",
            str(env_file),
            "--key",
            key,
        ],
        input=value,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_env_editor_updates_value_atomically_without_changing_mode(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MASTER_BOT_TOKEN=1:old\nMASTER_ADMIN_ID=10\nRUNTIME_SHARD_COUNT=4\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = _run(env_file, "MASTER_ADMIN_ID", "123456")
    assert result.returncode == 0, result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "MASTER_ADMIN_ID=123456" in text
    assert "MASTER_BOT_TOKEN=1:old" in text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_env_editor_never_prints_secret_value(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MASTER_BOT_TOKEN=1:old\n", encoding="utf-8")
    env_file.chmod(0o600)
    secret = "123456:VerySecretTokenValue"

    result = _run(env_file, "MASTER_BOT_TOKEN", secret)
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert secret in env_file.read_text(encoding="utf-8")


def test_env_editor_rejects_invalid_shard_count(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RUNTIME_SHARD_COUNT=4\n", encoding="utf-8")
    env_file.chmod(0o600)

    result = _run(env_file, "RUNTIME_SHARD_COUNT", "65")
    assert result.returncode != 0
    assert env_file.read_text(encoding="utf-8") == "RUNTIME_SHARD_COUNT=4\n"


def test_installer_does_not_pass_secret_values_via_env_command_arguments() -> None:
    content = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "env WL_ENV_VALUE=" not in content
    assert "env WL_BOT_TOKEN=" not in content
    assert "printf '%s' \"$token\"" in content
