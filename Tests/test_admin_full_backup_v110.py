"""v1.1.0: complete Tenant AdminBot full backup and panel artifacts."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from TenantRuntime.AdminBot import handlers
from TenantRuntime.backup import (
    BACKUP_FORMAT,
    create_tenant_full_backup,
    decode_tenant_backup,
)
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.panels import PanelTarget
from TenantRuntime.xnet import XnetPanelAdapter
from TenantRuntime.xui import XuiPanelAdapter


class FakeStateStore:
    def __init__(self) -> None:
        self.data = {}

    def load(self, user_id: int):
        return dict(self.data.get(int(user_id), {}))

    def save(self, user_id: int, value: dict):
        self.data[int(user_id)] = dict(value)


class AllowPolicy:
    def check(self, spec, *, telegram_user_id):
        return SimpleNamespace(allowed=True, license_status="active")


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return self


class FakeBot:
    def __init__(self) -> None:
        self.documents: list[dict] = []
        self.messages: list[dict] = []

    async def send_document(self, **kwargs):
        document = kwargs["document"]
        pos = document.tell()
        raw = document.read()
        document.seek(pos)
        self.documents.append({**kwargs, "raw": raw})
        return SimpleNamespace()

    async def send_message(self, **kwargs):
        self.messages.append(dict(kwargs))
        return SimpleNamespace()


class BackupPanel:
    def server_backup(self, *, target, secret):
        assert secret
        return {
            "filename": "panel-backup.bin",
            "content": f"backup:{target.endpoint}".encode(),
            "source_url": target.endpoint + "/backup",
        }


def _tenant_service(conn, factories, cipher, *, owner=7001, panel=None):
    tenant = factories.tenant(owner_telegram_id=owner)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    return tenant, service


def test_full_backup_keeps_tenant_restore_payload_and_adds_panel_artifacts(
    conn, factories, cipher, tmp_path
):
    tenant, service = _tenant_service(conn, factories, cipher)
    owner = int(tenant["owner_telegram_id"])
    server = service.add_server(
        owner,
        label="Turkey",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        admin_path="adm",
        user_path="usr",
    )
    service.set_panel_credential(
        owner,
        server_id=int(server["id"]),
        secret="tenant-one-secret",
    )
    service.register_customer(
        7101,
        display_name="Tenant One",
        username="one",
    )

    other, other_service = _tenant_service(
        conn, factories, cipher, owner=8001
    )
    other_service.register_customer(
        8101,
        display_name="Tenant Two",
        username="two",
    )

    artifact = create_tenant_full_backup(
        conn,
        tenant_id=int(tenant["id"]),
        panel_backups=[
            {
                "server_id": int(server["id"]),
                "server_label": "Turkey",
                "provider": "hiddify",
                "filename": "tr.json",
                "content": b'{"backup":true}',
                "source_url": "https://tr.example/adm/admin/backup/backupfile",
            }
        ],
        panel_errors=["Offline (#99): connection failed"],
        app_version="1.1.0",
    )

    assert artifact.filename.startswith("Backup_All_")
    assert artifact.filename.endswith(".zip")
    assert artifact.panel_backups_count == 1
    assert len(artifact.panel_errors) == 1

    with zipfile.ZipFile(io.BytesIO(artifact.data), "r") as archive:
        names = set(archive.namelist())
        assert {
            "tenant.json",
            "manifest.json",
            "full_manifest.json",
            "tenant_bot.db",
            "Shared/tenant.json",
            "Shared/manifest.json",
            "Shared/servers.json",
            "Shared/plans.json",
        } <= names
        assert any(
            name.startswith("Backup_Bot_") and name.endswith(".json")
            for name in names
        )
        assert any(
            name.startswith("Backup_All_") and name.endswith(".json")
            for name in names
        )
        assert "PanelBackups/Turkey/tr.json" in names
        db_path = tmp_path / "tenant_bot.db"
        db_path.write_bytes(archive.read("tenant_bot.db"))
        db = sqlite3.connect(db_path)
        try:
            own = db.execute(
                "SELECT COUNT(*) FROM tenant_customers WHERE tenant_id=?",
                (int(tenant["id"]),),
            ).fetchone()[0]
            foreign = db.execute(
                "SELECT COUNT(*) FROM tenant_customers WHERE tenant_id=?",
                (int(other["id"]),),
            ).fetchone()[0]
            bots = db.execute(
                "SELECT COUNT(*) FROM tenant_bots"
            ).fetchone()[0]
            assert own == 1
            assert foreign == 0
            assert bots == 0
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            db.close()
        full_manifest = json.loads(
            archive.read("full_manifest.json").decode("utf-8")
        )
        assert full_manifest["app_version"] == "1.1.0"
        assert full_manifest["panel_backups_count"] == 1
        assert full_manifest["panel_errors_count"] == 1
        assert full_manifest["security"]["tenant_scoped"] is True
        assert (
            full_manifest["security"]["live_tenant_bot_credentials_included"]
            is False
        )

    snapshot = decode_tenant_backup(artifact.data)
    assert snapshot["format"] == BACKUP_FORMAT
    assert int(snapshot["tenant_id"]) == int(tenant["id"])
    assert "tenant_bots" not in snapshot["tables"]
    for rows in snapshot["tables"].values():
        for row in rows:
            if "tenant_id" in row:
                assert int(row["tenant_id"]) == int(tenant["id"])
    customer_names = {
        row["display_name"]
        for row in snapshot["tables"]["tenant_customers"]
    }
    assert customer_names == {"Tenant One"}
    assert int(other["id"]) != int(tenant["id"])


def test_hiddify_backup_uses_cross_version_binary_route() -> None:
    body = b'{"users":[]}'
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/adm/admin/backup/backupfile"):
            return httpx.Response(
                200,
                content=body,
                headers={
                    "content-type": "application/json",
                    "content-disposition": 'attachment; filename="hiddify.json"',
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        "hiddify",
        "https://panel.example",
        admin_path="adm",
        user_path="usr",
    )
    result = adapter.server_backup(target=target, secret="key")
    assert result["content"] == body
    assert result["filename"] == "hiddify.json"
    assert any(path.endswith("/adm/admin/backup/backupfile") for path in seen)


def test_xui_backup_downloads_database_for_sanaei() -> None:
    body = b"sqlite-backup"

    def handler(request):
        path = request.url.path
        if path.endswith("/panel/api/inbounds/list"):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/panel/api/server/getDb"):
            return httpx.Response(
                200,
                content=body,
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="xui.db"',
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        "xui",
        "https://xui.example",
        xui_flavor="sanaei",
    )
    result = adapter.server_backup(target=target, secret="api-token")
    assert result["content"] == body
    assert result["filename"] == "xui.db"
    assert result["source_url"].endswith("/panel/api/server/getDb")


def test_xnet_backup_creates_downloads_and_cleans_up() -> None:
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/api/v1/ping"):
            return httpx.Response(200, json={"ok": True}, request=request)
        if path.endswith("/api/backups") and request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "b-1", "filename": "xnet.db"},
                request=request,
            )
        if path.endswith("/api/backups/b-1/download"):
            return httpx.Response(
                200,
                content=b"xnet-backup",
                headers={"content-type": "application/octet-stream"},
                request=request,
            )
        if path.endswith("/api/backups/b-1") and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(404, request=request)

    adapter = XnetPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget("xnet", "https://xnet.example")
    result = adapter.server_backup(target=target, secret="api-token")
    assert result["content"] == b"xnet-backup"
    assert result["filename"] == "xnet.db"
    assert ("POST", "/api/backups") in calls
    assert ("GET", "/api/backups/b-1/download") in calls
    assert ("DELETE", "/api/backups/b-1") in calls


def test_admin_backup_button_sends_full_archive_and_records_success(
    monkeypatch, conn, factories, cipher
):
    tenant, service = _tenant_service(
        conn,
        factories,
        cipher,
        panel=BackupPanel(),
    )
    owner = int(tenant["owner_telegram_id"])
    server = service.add_server(
        owner,
        label="France",
        panel_kind="hiddify",
        endpoint="https://fr.example",
        admin_path="adm",
        user_path="usr",
    )
    service.set_panel_credential(
        owner,
        server_id=int(server["id"]),
        secret="fr-secret",
    )
    service.register_customer(
        7101,
        display_name="Manual Backup",
        username="manual",
    )

    state = FakeStateStore()
    spec = SimpleNamespace(
        role="admin",
        tenant_id=int(tenant["id"]),
        tenant_name="Tenant",
    )
    monkeypatch.setattr(
        handlers,
        "_services",
        lambda context: (spec, AllowPolicy(), state, service),
    )

    message = FakeMessage(handlers.BTN_BACKUP)
    bot = FakeBot()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=owner),
        effective_message=message,
        effective_chat=SimpleNamespace(id=owner),
        callback_query=None,
    )
    context = SimpleNamespace(bot=bot, user_data={})

    asyncio.run(handlers.send_admin_full_backup(update, context))

    assert message.replies[0][0] == (
        "⏳ در حال تهیه بکاپ کامل (ربات + سرورها/نودها)..."
    )
    assert len(bot.documents) == 1
    sent = bot.documents[0]
    assert sent["filename"].startswith("Backup_All_")
    assert sent["caption"].startswith("📬 بکاپ کامل\n🕐 زمان: ")
    assert "🤖 بکاپ ربات: ✅" in sent["caption"]
    assert "🖥️ بکاپ سرورها/نودها: 1 مورد" in sent["caption"]
    assert "⚠️ خطاها: 0 مورد" in sent["caption"]
    snapshot = decode_tenant_backup(sent["raw"])
    assert int(snapshot["tenant_id"]) == int(tenant["id"])
    with zipfile.ZipFile(io.BytesIO(sent["raw"]), "r") as archive:
        names = set(archive.namelist())
        assert "PanelBackups/France/panel-backup.bin" in names
        assert "tenant_bot.db" in names
        assert "Shared/tenant.json" in names
        assert any(name.startswith("Backup_Bot_") for name in names)
        assert any(name.startswith("Backup_All_") and name.endswith(".json") for name in names)

    row = conn.execute(
        "SELECT status,file_size,sha256 FROM tenant_backup_runs "
        "WHERE tenant_id=? AND slot_key LIKE 'manual:%' "
        "ORDER BY started_at DESC LIMIT 1",
        (int(tenant["id"]),),
    ).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert int(row["file_size"]) == len(sent["raw"])
    assert len(str(row["sha256"])) == 64
    assert state.load(owner)["screen"] == "backup_complete"


def test_full_backup_decoder_rejects_unsafe_extra_members(
    conn, factories, cipher
):
    tenant, _ = _tenant_service(conn, factories, cipher)
    base = create_tenant_full_backup(
        conn,
        tenant_id=int(tenant["id"]),
    )
    with zipfile.ZipFile(io.BytesIO(base.data), "r") as src:
        tenant_json = src.read("tenant.json")
        manifest = src.read("manifest.json")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.writestr("tenant.json", tenant_json)
        dst.writestr("manifest.json", manifest)
        dst.writestr("../secret.txt", b"bad")
    with pytest.raises(Exception, match="unsafe|layout"):
        decode_tenant_backup(out.getvalue())
