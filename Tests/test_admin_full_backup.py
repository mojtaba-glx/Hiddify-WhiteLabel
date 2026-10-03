"""Native downloads, menu delivery, isolation and full-archive restore compatibility."""

import asyncio
import gzip
import io
import json
import threading
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import NetworkError

from TenantRuntime import panel_backups
from TenantRuntime.AdminBot import full_backup, handlers
from TenantRuntime.backup import (
    TenantBackupError,
    decode_tenant_backup,
    restore_tenant_backup,
)
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.panels import PanelError, PanelTarget, RoutedPanelAdapter
from TenantRuntime.xui import XuiPanelAdapter
from TenantRuntime.xnet import XnetPanelAdapter
from Tests.test_server_management_complete import setup
from Tests.test_server_connection_parity import Messages

HIDDIFY = PanelTarget(
    kind="hiddify", endpoint="https://panel.example", admin_path="secret-admin"
)
JSON_BACKUP = b'{"users":[],"domains":[],"hconfigs":{}}'
SQLITE_BACKUP = b"SQLite format 3\x00" + bytes(100)


def test_hiddify_native_backup_preserves_json_filename_and_api_key():
    calls = []

    def transport(request):
        calls.append(request)
        assert request.headers["Hiddify-API-Key"] == "private-key"
        assert request.url.path == "/secret-admin/admin/backup/backupfile"
        return httpx.Response(
            200,
            content=gzip.compress(JSON_BACKUP),
            headers={
                "content-encoding": "gzip",
                "content-type": "application/json",
                "content-disposition": "attachment; filename*=UTF-8''Hiddify%20Backup.json",
            },
        )

    adapter = RoutedPanelAdapter(
        {"hiddify": HiddifyPanelAdapter(transport=httpx.MockTransport(transport))}
    )
    result = adapter.download_backup(target=HIDDIFY, secret="private-key")
    assert result.content == JSON_BACKUP and result.filename == "Hiddify Backup.json"
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("form", [False, True])
def test_hiddify_html_fallback_retains_session_and_ignores_unsafe_actions(form):
    calls = []

    def transport(request):
        calls.append((request.method, str(request.url)))
        assert request.url.host == "panel.example"
        path = request.url.path
        if path.endswith("/admin/backup"):
            safe = (
                (
                    '<form action="/secret-admin/admin/backup/download" method="post">'
                    '<input type="hidden" name="csrf" value="abc"></form>'
                )
                if form
                else ('<a href="/secret-admin/admin/backup/download">Download</a>')
            )
            return httpx.Response(
                200,
                text=(
                    '<a href="https://other.example/download">bad</a>'
                    '<a href="/secret-admin/admin/backup/delete.json">bad</a>'
                    '<form action="/secret-admin/admin/backup/restore" method="post"></form>'
                    + safe
                ),
                headers={"set-cookie": "session=test; Path=/"},
            )
        if path.endswith("/download"):
            assert request.headers["cookie"] == "session=test"
            assert request.method == ("POST" if form else "GET")
            if form:
                assert request.content == b"csrf=abc"
            return httpx.Response(200, content=JSON_BACKUP)
        return httpx.Response(404)

    result = HiddifyPanelAdapter(
        transport=httpx.MockTransport(transport)
    ).download_backup(target=HIDDIFY, secret="key")
    assert result.content == JSON_BACKUP
    assert not any("restore" in url or "delete" in url for _, url in calls)


@pytest.mark.parametrize(
    "body", [b"", b"<html>login</html>", b'{"success":false}', b"{}", b"not-json"]
)
def test_hiddify_rejects_empty_html_and_json_error_responses(body):
    adapter = HiddifyPanelAdapter(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )
    with pytest.raises(PanelError):
        adapter.download_backup(target=HIDDIFY, secret="key")


def test_hiddify_rejects_cross_origin_redirect_without_sending_key():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"location": "https://other.example/backupfile"}
        )

    with pytest.raises(PanelError):
        HiddifyPanelAdapter(transport=httpx.MockTransport(transport)).download_backup(
            target=HIDDIFY, secret="key"
        )
    assert len(calls) == 1


def test_panel_download_bounds_decompressed_size(monkeypatch):
    monkeypatch.setattr(panel_backups, "MAX_PANEL_BACKUP_BYTES", 32)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=gzip.compress(b"a" * 1000),
                headers={"content-encoding": "gzip"},
            )
        )
    )
    with client, pytest.raises(PanelError, match="size limit"):
        panel_backups.bounded_request(client, "GET", "https://panel.example/backup")


@pytest.mark.parametrize("flavor", ["sanaei", "alireza"])
def test_xui_native_database_uses_correct_flavor_and_auth(flavor):
    def transport(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "session=ok; Path=/"},
            )
        if request.url.path.endswith("/inbounds/list"):
            return httpx.Response(200, json={"success": True, "obj": []})
        assert request.method == "GET"
        expected = (
            "/base/panel/api/server/getDb"
            if flavor == "sanaei"
            else "/base/xui/API/server/getDb"
        )
        assert request.url.path == expected
        if flavor == "sanaei":
            assert request.headers["authorization"] == "Bearer api-key"
        else:
            assert request.headers["cookie"] == "session=ok"
            assert request.headers["XUI-Xray-App-Secret-Key"] == "header-key"
        return httpx.Response(200, content=SQLITE_BACKUP)

    target = PanelTarget(
        kind="xui", endpoint="https://panel.example/base", xui_flavor=flavor
    )
    secret = "api-key" if flavor == "sanaei" else "admin|password|header-key"
    result = XuiPanelAdapter(transport=httpx.MockTransport(transport)).download_backup(
        target=target, secret=secret
    )
    assert result.content == SQLITE_BACKUP and result.filename.endswith(".db")


def test_xnet_download_auth_fallback_and_failed_cleanup_preserves_artifact():
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/ping"):
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"token": "jwt"})
        if path.endswith("/backups"):
            return httpx.Response(
                200, json={"data": {"id": "new", "filename": "xnet.db"}}
            )
        if path.endswith("/download"):
            if request.headers["authorization"] == "Bearer api-key":
                return httpx.Response(403)
            assert request.headers["authorization"] == "Bearer jwt"
            return httpx.Response(200, content=SQLITE_BACKUP)
        assert request.method == "DELETE" and path.endswith("/backups/new")
        return httpx.Response(500)

    result = XnetPanelAdapter(transport=httpx.MockTransport(transport)).download_backup(
        target=PanelTarget(kind="xnet", endpoint="https://xnet.example"),
        secret=json.dumps(
            {"api_token": "api-key", "username": "admin", "password": "password"}
        ),
    )
    assert result.content == SQLITE_BACKUP and result.filename == "xnet.db"
    assert calls[-1] == ("DELETE", "/api/backups/new")


class BackupMessages(Messages):
    id = 7001

    def __init__(self):
        super().__init__()
        self.documents = []
        self.fail = False

    async def send_document(self, *, document, **kwargs):
        if self.fail:
            raise NetworkError("send failed")
        self.documents.append((document.read(), kwargs))


@pytest.mark.parametrize("has_new", [True, False])
def test_xnet_acknowledgement_only_downloads_new_inventory_entry(has_new):
    calls = []
    created = False

    def transport(request):
        nonlocal created
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/ping"):
            return httpx.Response(200, json={})
        if path.endswith("/backups"):
            if request.method == "POST":
                created = True
                return httpx.Response(200, json={"success": True})
            rows = [{"id": "old", "filename": "old.db"}]
            if created and has_new:
                rows.append({"id": "fresh", "filename": "fresh.db"})
            return httpx.Response(200, json={"backups": rows})
        assert path.endswith("/backups/fresh/download") and request.method == "GET"
        return httpx.Response(200, content=SQLITE_BACKUP)

    adapter = XnetPanelAdapter(transport=httpx.MockTransport(transport))
    target = PanelTarget(kind="xnet", endpoint="https://xnet.example")
    if has_new:
        assert (
            adapter.download_backup(target=target, secret="key").filename == "fresh.db"
        )
    else:
        with pytest.raises(PanelError, match="identity"):
            adapter.download_backup(target=target, secret="key")
    assert not any(
        method == "DELETE" or "old/download" in path for method, path in calls
    )


def ui(monkeypatch, business):
    message = BackupMessages()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7001),
        effective_message=message,
        effective_chat=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(
        handlers,
        "_services",
        lambda _: (SimpleNamespace(role="admin"), None, None, business),
    )
    return update, context, message


def test_backup_menu_sends_tenant_and_native_panels_with_roundtrip_restore(
    monkeypatch, conn, factories, cipher
):
    business, _, _, source, child, _, _ = setup(conn, factories, cipher)
    other = factories.tenant(owner_telegram_id=8001)
    calls, thread_ids = [], []

    class Panel:
        def download_backup(self, *, target, secret):
            calls.append(target.endpoint)
            thread_ids.append(threading.get_ident())
            return panel_backups.PanelBackup("../../native.json", JSON_BACKUP)

    business.panel_adapter = Panel()
    conn.execute(
        "UPDATE tenant_servers SET status='disabled' WHERE id=?", (child["id"],)
    )
    conn.commit()
    update, context, message = ui(monkeypatch, business)
    message.text = handlers.BTN_BACKUP
    asyncio.run(handlers.unknown_text(update, context))
    assert len(message.documents) == 1
    data, kw = message.documents[0]
    assert kw["filename"].startswith("Backup_All_Tenant_")
    assert "سرورها/نودها: 2 مورد" in kw["caption"] and "خطاها: 0" in kw["caption"]
    assert set(calls) == {source["endpoint"], child["endpoint"]}
    assert all(t != threading.get_ident() for t in thread_ids)
    snapshot = decode_tenant_backup(data)
    assert snapshot["tenant_id"] == business.tenant_id != other["id"]
    assert "tenant_bots" not in snapshot["tables"]
    assert all(
        r["tenant_id"] == business.tenant_id
        for rows in snapshot["tables"].values()
        for r in rows
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for item in manifest["panel_backups"]:
            assert archive.read(item["path"]) == JSON_BACKUP
            assert ".." not in item["path"] and item["path"].endswith("/native.json")
    report = restore_tenant_backup(conn, tenant_id=business.tenant_id, data=data)
    assert report.rows_restored > 0
    with pytest.raises(TenantBackupError):
        restore_tenant_backup(conn, tenant_id=other["id"], data=data)
    assert "full_backup_running" not in context.user_data


def test_panel_failure_keeps_bot_and_healthy_backup_without_leaking_secret(
    monkeypatch, conn, factories, cipher
):
    business, _, _, source, child, _, _ = setup(conn, factories, cipher)

    class Panel:
        def download_backup(self, *, target, secret):
            if target.endpoint == child["endpoint"]:
                raise RuntimeError("secret-value https://private.example/private-path")
            return panel_backups.PanelBackup("native.json", JSON_BACKUP)

    business.panel_adapter = Panel()
    update, context, message = ui(monkeypatch, business)
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert len(message.documents) == 1
    data, kw = message.documents[0]
    assert "سرورها/نودها: 1 مورد" in kw["caption"] and "خطاها: 1" in kw["caption"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = archive.read("manifest.json").decode()
        assert "secret-value" not in manifest and "private-path" not in manifest
    assert "secret-value" not in str(message.sent)
    assert child["label"] in message.sent[-1][0]


def test_backup_rejects_nonowner_group_and_duplicate_and_recovers_send_failure(
    monkeypatch, conn, factories, cipher
):
    business, _, _, _, _, _, _ = setup(conn, factories, cipher)
    update, context, message = ui(monkeypatch, business)
    with pytest.raises(PermissionError):
        asyncio.run(full_backup.send_full_backup(update, context, business, 8001))
    message.id = -123
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert not message.documents and "خصوصی" in message.sent[-1][0]
    message.id = 7001
    context.user_data["full_backup_running"] = True
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert "قبلی" in message.sent[-1][0]
    context.user_data.clear()
    message.fail = True
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert "ارسال فایل بکاپ ناموفق" in message.sent[-1][0]
    assert "full_backup_running" not in context.user_data


def test_full_archive_rejects_tampered_panel_bytes(
    monkeypatch, conn, factories, cipher
):
    business, _, _, source, _, _, _ = setup(conn, factories, cipher)
    from TenantRuntime.backup import build_tenant_snapshot

    _, original = full_backup.build_full_archive(
        build_tenant_snapshot(conn, tenant_id=business.tenant_id),
        [(source, panel_backups.PanelBackup("native.json", JSON_BACKUP))],
        [],
    )
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            dst.writestr(
                name, b"changed" if name.startswith("PanelBackups/") else src.read(name)
            )
    with pytest.raises(TenantBackupError, match="checksum"):
        decode_tenant_backup(out.getvalue())


def test_backup_without_servers_and_size_error_leave_button_reusable(
    monkeypatch, conn, factories, cipher
):
    from Tests.test_backup_shared_infra_phase15 import _tenant_service

    _, business, *_ = _tenant_service(conn, factories, cipher)
    update, context, message = ui(monkeypatch, business)
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert len(message.documents) == 1
    assert "سرورها/نودها: 0 مورد" in message.documents[0][1]["caption"]
    assert (
        decode_tenant_backup(message.documents[0][0])["tenant_id"] == business.tenant_id
    )
    monkeypatch.setattr(full_backup, "MAX_TELEGRAM_BACKUP_BYTES", 1)
    asyncio.run(full_backup.send_full_backup(update, context, business, 7001))
    assert len(message.documents) == 1 and "حد مجاز" in message.sent[-1][0]
    assert "full_backup_running" not in context.user_data
