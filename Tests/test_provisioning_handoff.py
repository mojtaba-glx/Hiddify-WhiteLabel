"""One-time webhook handoff has strict permissions and no bot tokens."""

from __future__ import annotations

import json
import stat

import pytest

from Provisioning.handoff import InsecureHandoffDirectoryError, write_secret_handoff
from Provisioning.service import (
    ProvisionedBotInfo,
    ProvisioningResult,
    WebhookSecrets,
)


def _result() -> ProvisioningResult:
    return ProvisioningResult(
        tenant_id=4,
        tenant_public_id="public-safe-id",
        data_namespace="namespace-safe-id",
        admin_bot=ProvisionedBotInfo(1, "admin", 7001, "admin_bot", "aaaa"),
        user_bot=ProvisionedBotInfo(2, "user", 7002, "user_bot", "bbbb"),
        webhook_secrets=WebhookSecrets(admin="admin-secret-value", user="user-secret-value"),
    )


def test_handoff_directory_and_file_are_private_and_exclusive(tmp_path) -> None:
    directory = tmp_path / "handoff"
    result = _result()
    path = write_secret_handoff(result, directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["admin_bot"]["webhook_secret"] == "admin-secret-value"
    assert "token" not in path.read_text(encoding="utf-8").lower()
    with pytest.raises(FileExistsError):
        write_secret_handoff(result, directory)


def test_handoff_rejects_existing_public_directory(tmp_path) -> None:
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(InsecureHandoffDirectoryError):
        write_secret_handoff(_result(), directory)
    assert not list(directory.iterdir())
