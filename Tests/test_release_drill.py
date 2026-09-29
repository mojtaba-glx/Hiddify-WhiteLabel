from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from Ops import release_drill


def _repo(tmp_path: Path) -> Path:
    files = [
        "requirements.txt",
        "install.sh",
        "bootstrap.sh",
        "scripts/migrate.py",
        "Tests/test_hiddify_live.py",
        "Tests/test_xui_live.py",
        "Tests/test_xnet_live.py",
        "Tests/test_subscription_lifecycle.py",
        "Tests/test_multinode_smart_subscription.py",
        "Tests/test_reminder_global_enforcer.py",
        "Tests/test_tenant_reports_admin_user.py",
        "Tests/test_tenant_sales_growth.py",
        "Tests/test_customer_purchase_e2e.py",
        "Tests/test_update_snapshot.py",
        "Tests/test_ops_health.py",
        "Tests/test_install_script.py",
    ]
    for raw in files:
        path = tmp_path / raw
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    return tmp_path


def test_plan_is_offline_and_covers_release_critical_groups(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    steps = release_drill.build_offline_plan(repo)
    names = [step.name for step in steps]
    assert names == [
        "provider-contracts",
        "subscription-lifecycle",
        "tenant-commerce-and-admin",
        "operations-and-rollback",
        "install-shell-syntax",
        "bootstrap-shell-syntax",
        "fresh-migration-smoke",
    ]
    joined = " ".join(part for step in steps for part in step.argv)
    assert "http://" not in joined and "https://" not in joined
    assert "test_hiddify_live.py" in joined
    assert "test_multinode_smart_subscription.py" in joined
    assert "test_tenant_sales_growth.py" in joined
    assert "test_update_snapshot.py" in joined


def test_full_suite_plan_uses_single_pytest_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    steps = release_drill.build_offline_plan(
        repo,
        full_suite=True,
    )
    assert steps[0].name == "full-offline-suite"
    assert steps[0].argv == (
        sys.executable,
        "-m",
        "pytest",
        "-q",
    )
    assert steps[-1].name == "fresh-migration-smoke"


def test_missing_release_input_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="repository is incomplete",
    ):
        release_drill.build_offline_plan(tmp_path)


def test_runner_stops_on_first_failure(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    seen = []

    def fake_runner(argv, **kwargs):
        seen.append((tuple(argv), kwargs))
        code = 1 if len(seen) == 2 else 0
        output = "ok\n" if code == 0 else "boom\n"
        return subprocess.CompletedProcess(
            argv,
            code,
            stdout=output,
        )

    report = release_drill.run_offline_drill(
        repo,
        runner=fake_runner,
    )
    assert report.ok is False
    assert [r.name for r in report.results] == [
        "provider-contracts",
        "subscription-lifecycle",
    ]
    assert report.results[-1].output_tail == "boom"


def test_runner_expands_private_temp_db_and_succeeds(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    seen = []

    def fake_runner(argv, **kwargs):
        seen.append(tuple(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="pass\n",
        )

    report = release_drill.run_offline_drill(
        repo,
        runner=fake_runner,
    )
    assert report.ok is True
    migration = next(
        argv
        for argv in seen
        if "migrate.py" in " ".join(argv)
    )
    db_path = Path(migration[-1])
    assert db_path.name == "fresh.db"
    assert "whitelabel-release-drill-" in str(db_path.parent)
    assert all(
        result.output_tail == "pass"
        for result in report.results
    )
