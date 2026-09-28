"""Write the one-time webhook handoff without printing secret values."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from Provisioning.service import ProvisioningResult


class InsecureHandoffDirectoryError(PermissionError):
    """The handoff directory is not private and will not be modified."""


def _private_directory(path: Path) -> Path:
    if path.exists():
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise InsecureHandoffDirectoryError(
                "handoff directory must already have mode 0700"
            )
        return path
    if not path.parent.exists():
        raise InsecureHandoffDirectoryError("handoff parent directory does not exist")
    path.mkdir(mode=0o700)
    return path


def write_secret_handoff(
    result: ProvisioningResult, directory: str | Path
) -> Path:
    """Create a new 0600 JSON handoff file; never overwrite an existing file."""
    target_dir = _private_directory(Path(directory))
    path = target_dir / f"tenant-{result.tenant_public_id}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    payload = {
        "tenant_id": result.tenant_id,
        "tenant_public_id": result.tenant_public_id,
        "data_namespace": result.data_namespace,
        "admin_bot": {
            "telegram_bot_id": result.admin_bot.telegram_bot_id,
            "username": result.admin_bot.telegram_username,
            "webhook_secret": result.webhook_secrets.admin,
        },
        "user_bot": {
            "telegram_bot_id": result.user_bot.telegram_bot_id,
            "username": result.user_bot.telegram_username,
            "webhook_secret": result.webhook_secrets.user,
        },
        "instruction": "Configure the webhooks, then securely delete this file.",
    }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return path
