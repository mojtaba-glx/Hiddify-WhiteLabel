"""Generated units use fixed shard instances and safe service settings."""

from __future__ import annotations

import stat

import pytest

from Ops.systemd import (
    MASTER_UNIT,
    RUNTIME_UNIT,
    render_master_unit,
    render_runtime_unit,
    write_units,
)


def test_units_point_to_project_venv_and_fixed_runtime_index(tmp_path) -> None:
    project = tmp_path / "white-label"
    project.mkdir()
    master = render_master_unit(project, "mojte")
    runtime = render_runtime_unit(project, "mojte")
    assert f"WorkingDirectory={project}" in master
    assert f"EnvironmentFile={project}/.env" in master
    assert f"ExecStart={project}/.venv/bin/python -m MasterBot" in master
    assert "UMask=0077" in master and "NoNewPrivileges=true" in master
    assert "RUNTIME_SHARD_INDEX=%i" in runtime
    assert f"{project}/.venv/bin/python -m TenantRuntime" in runtime
    assert "MASTER_BOT_TOKEN" not in master + runtime


def test_unit_write_is_deterministic_and_public_only(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    master, runtime = write_units(tmp_path / "units", project_root=project, service_user="bot_user")
    assert (master.name, runtime.name) == (MASTER_UNIT, RUNTIME_UNIT)
    assert stat.S_IMODE(master.stat().st_mode) == 0o644
    before = master.read_text(encoding="utf-8")
    write_units(tmp_path / "units", project_root=project, service_user="bot_user")
    assert master.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("user", ["root;bad", "UPPER", "", "x" * 40])
def test_invalid_service_user_rejected(tmp_path, user) -> None:
    with pytest.raises(ValueError):
        render_master_unit(tmp_path, user)


def test_project_path_with_whitespace_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        render_runtime_unit(tmp_path / "bad path", "root")
