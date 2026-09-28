"""Render deterministic systemd units without invoking systemctl."""

from __future__ import annotations

import os
import re
from pathlib import Path

MASTER_UNIT = "hiddify-whitelabel-master.service"
RUNTIME_UNIT = "hiddify-whitelabel-runtime@.service"


def _validate(project_root: Path | str, service_user: str) -> tuple[Path, str]:
    root = Path(project_root).resolve()
    user = str(service_user or "").strip()
    if not root.is_absolute() or any(char.isspace() for char in str(root)):
        raise ValueError("project path must be absolute and contain no whitespace")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("invalid service user")
    return root, user


def render_master_unit(project_root: Path | str, service_user: str) -> str:
    root, user = _validate(project_root, service_user)
    return f"""[Unit]
Description=Hiddify WhiteLabel MasterBot and License Jobs
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
EnvironmentFile={root}/.env
ExecStart={root}/.venv/bin/python -m MasterBot
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""


def render_runtime_unit(project_root: Path | str, service_user: str) -> str:
    root, user = _validate(project_root, service_user)
    return f"""[Unit]
Description=Hiddify WhiteLabel TenantRuntime shard %i
After=network-online.target hiddify-whitelabel-master.service
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
EnvironmentFile={root}/.env
ExecStart=/usr/bin/env RUNTIME_SHARD_INDEX=%i {root}/.venv/bin/python -m TenantRuntime
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""


def write_units(
    output_dir: Path | str, *, project_root: Path | str, service_user: str
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    master = directory / MASTER_UNIT
    runtime = directory / RUNTIME_UNIT
    for path, content in (
        (master, render_master_unit(project_root, service_user)),
        (runtime, render_runtime_unit(project_root, service_user)),
    ):
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    return master, runtime
