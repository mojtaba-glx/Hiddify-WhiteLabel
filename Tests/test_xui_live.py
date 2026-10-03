"""Live X-UI adapters for Sanaei and Alireza, fully offline with MockTransport."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import PanelError, PanelTarget, ProvisionRequest, RenewRequest
from TenantRuntime.xui import XuiPanelAdapter


def _request(*, key: str = "tenant:7:subscription:23") -> ProvisionRequest:
    return ProvisionRequest(
        tenant_id=7,
        server_id=11,
        subscription_id=23,
        customer_id=31,
        traffic_bytes=20 * 1024**3,
        duration_days=30,
        expires_at=iso_utc(utcnow() + timedelta(days=30)),
        idempotency_key=key,
    )


def _sanaei_target() -> PanelTarget:
    return PanelTarget(
        kind="xui",
        endpoint="https://sanaei.example",
        xui_flavor="sanaei",
        xui_inbound_ids="1,2",
        xui_public_origin="https://sub.example",
        xui_sub_path="/sub/",
    )


def test_sanaei_create_renew_state_usage_delete_and_native_link() -> None:
    state = {
        "clients": [],
        "reset_calls": 0,
        "add_calls": 0,
        "updates": [],
    }
    inbounds = [
        {
            "id": 1,
            "protocol": "vless",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
        {
            "id": 2,
            "protocol": "trojan",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
    ]

    def response(request: httpx.Request, payload) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer sanaei-token"
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "GET" and path.endswith("/panel/api/inbounds/list"):
            return response(request, {"success": True, "obj": inbounds})
        if request.method == "GET" and path.endswith("/panel/api/clients/list"):
            return response(request, {"success": True, "obj": state["clients"]})
        if request.method == "POST" and path.endswith("/panel/api/clients/add"):
            state["add_calls"] += 1
            client = dict(body["client"])
            # Model Sanaei's first-class DB row where id is numeric and uuid is
            # the Xray credential. This catches accidental numeric-id updates.
            xray_uuid = str(client.get("id") or client.get("password"))
            stored = dict(client)
            stored["id"] = 991
            stored["uuid"] = xray_uuid
            stored["inboundIds"] = list(body["inboundIds"])
            stored["traffic"] = {"up": 1024, "down": 2048}
            stored["lastOnline"] = 0
            state["clients"] = [stored]
            return response(request, {"success": True, "obj": stored})
        if request.method == "POST" and "/panel/api/clients/update/" in path:
            state["updates"].append(body)
            assert body["id"] != 991
            assert "-" in str(body["id"])
            current = dict(state["clients"][0])
            current.update(body)
            current["id"] = 991
            current["uuid"] = str(body.get("uuid") or body.get("id"))
            current["inboundIds"] = [1, 2]
            current.setdefault("traffic", {"up": 1024, "down": 2048})
            state["clients"] = [current]
            return response(request, {"success": True, "obj": current})
        if request.method == "POST" and "/panel/api/clients/resetTraffic/" in path:
            state["reset_calls"] += 1
            if state["clients"]:
                state["clients"][0]["traffic"] = {"up": 0, "down": 0}
            return response(request, {"success": True, "obj": True})
        if request.method == "POST" and path.endswith("/panel/api/clients/onlines"):
            emails = [str(x.get("email")) for x in state["clients"]]
            return response(request, {"success": True, "obj": emails})
        if request.method == "POST" and path.endswith("/panel/api/clients/lastOnline"):
            email = str(state["clients"][0]["email"]) if state["clients"] else ""
            return response(
                request,
                {"success": True, "obj": {email: 1790633400000} if email else {}},
            )
        if request.method == "POST" and "/panel/api/clients/del/" in path:
            state["clients"] = []
            return response(request, {"success": True, "obj": True})
        return httpx.Response(404, request=request)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    request = _request()
    created = adapter.provision(
        target=_sanaei_target(),
        secret="sanaei-token",
        request=request,
    )
    assert state["add_calls"] == 1
    assert created.external_ref
    assert created.subscription_url == (
        f"https://sub.example/sub/{created.external_ref}"
    )
    assert state["clients"][0]["inboundIds"] == [1, 2]
    assert state["clients"][0]["totalGB"] == 20 * 1024**3

    # Stable UUID makes a provisioning retry converge.
    retried = adapter.provision(
        target=_sanaei_target(),
        secret="sanaei-token",
        request=request,
    )
    assert retried.external_ref == created.external_ref
    assert state["add_calls"] == 1

    snapshot = adapter.get_user(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
    )
    assert snapshot.usage_bytes == 3072
    assert snapshot.active is True
    assert snapshot.last_online  # online endpoint takes precedence

    inventory=adapter.list_users(target=_sanaei_target(),secret="sanaei-token")
    assert len(inventory)==1 and inventory[0]['external_ref']==created.external_ref
    adapter.update_user(target=_sanaei_target(),secret="sanaei-token",external_ref=created.external_ref,
                        changes={'name':'Friendly native name','comment':'Operator note','traffic_bytes':25*1024**3})
    current=adapter.get_user(target=_sanaei_target(),secret="sanaei-token",external_ref=created.external_ref)
    assert current.usage_bytes==3072 and current.active is True and state['reset_calls']==0
    assert state['clients'][0]['id']==991 and state['clients'][0]['uuid']==created.external_ref
    assert adapter.list_users(target=_sanaei_target(),secret="sanaei-token")[0]['name']=='Friendly native name'

    renewal = RenewRequest(
        traffic_bytes=50 * 1024**3,
        duration_days=45,
        expires_at=iso_utc(utcnow() + timedelta(days=45)),
        idempotency_key="tenant:7:renewal-order:88",
    )
    renewed = adapter.renew(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
        request=renewal,
    )
    assert renewed.traffic_bytes == 50 * 1024**3
    assert renewed.usage_bytes == 0
    assert state["reset_calls"] == 1
    assert "wl-renew:" in str(state["clients"][0]["comment"])

    # Traffic after the first remote renewal must survive an identical retry.
    state["clients"][0]["traffic"] = {"up": 2 * 1024**3, "down": 0}
    retry_renew = adapter.renew(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
        request=renewal,
    )
    assert state["reset_calls"] == 1
    assert retry_renew.usage_bytes == 2 * 1024**3

    disabled = adapter.set_enabled(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
        enabled=False,
    )
    assert disabled.active is False
    enabled = adapter.set_enabled(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
        enabled=True,
    )
    assert enabled.active is True

    usage = adapter.usage(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
    )
    assert usage.usage_bytes == 2 * 1024**3
    assert usage.active is True

    adapter.delete_user(
        target=_sanaei_target(),
        secret="sanaei-token",
        external_ref=created.external_ref,
    )
    assert state["clients"] == []


def _alireza_target() -> PanelTarget:
    return PanelTarget(
        kind="xui",
        endpoint="https://alireza.example/base",
        xui_flavor="alireza",
        xui_inbound_ids="1,2",
        xui_public_origin="sub-alireza.example",
        xui_sub_path="sub",
    )


def test_alireza_multi_inbound_create_retry_renew_and_delete() -> None:
    inbounds = [
        {
            "id": 1,
            "protocol": "vless",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
        {
            "id": 2,
            "protocol": "trojan",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
    ]
    state = {
        "login_calls": 0,
        "add_calls": 0,
        "reset_calls": 0,
        "header_seen": False,
    }

    def response(request: httpx.Request, payload, *, cookie: bool = False) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if cookie:
            headers["set-cookie"] = "session=test-cookie; Path=/"
        return httpx.Response(
            200,
            request=request,
            headers=headers,
            content=json.dumps(payload).encode(),
        )

    def replace_inbound_client(inbound_id: int, route_id: str, client: dict) -> None:
        for inbound in inbounds:
            if int(inbound["id"]) != inbound_id:
                continue
            settings = json.loads(inbound["settings"])
            clients = list(settings.get("clients") or [])
            found = False
            for index, current in enumerate(clients):
                values = {
                    str(current.get("id") or ""),
                    str(current.get("password") or ""),
                    str(current.get("email") or ""),
                }
                if route_id in values:
                    clients[index] = client
                    found = True
                    break
            if not found:
                clients.append(client)
            settings["clients"] = clients
            inbound["settings"] = json.dumps(settings)
            emails = {str(x.get("email")) for x in clients}
            inbound["clientStats"] = [
                row
                for row in inbound.get("clientStats", [])
                if str(row.get("email")) in emails
            ]
            for item in clients:
                email = str(item.get("email"))
                if not any(
                    str(row.get("email")) == email
                    for row in inbound["clientStats"]
                ):
                    inbound["clientStats"].append(
                        {"email": email, "up": 0, "down": 0}
                    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "POST" and path.endswith("/base/login"):
            state["login_calls"] += 1
            assert body == {"username": "admin", "password": "pass"}
            return response(request, {"success": True}, cookie=True)

        if "/base/xui/API/" in path:
            assert "session=test-cookie" in request.headers.get("cookie", "")
            if request.headers.get("XUI-Xray-App-Secret-Key") == "header-secret":
                state["header_seen"] = True

        if request.method == "GET" and path.endswith("/base/xui/API/inbounds/"):
            return response(request, {"success": True, "obj": inbounds})
        if request.method == "POST" and path.endswith("/base/xui/API/inbounds/addClient"):
            state["add_calls"] += 1
            inbound_id = int(body["id"])
            client = json.loads(body["settings"])["clients"][0]
            replace_inbound_client(inbound_id, "", client)
            return response(request, {"success": True, "obj": True})
        if request.method == "POST" and "/base/xui/API/inbounds/updateClient/" in path:
            inbound_id = int(body["id"])
            route_id = path.rsplit("/", 1)[-1]
            client = json.loads(body["settings"])["clients"][0]
            replace_inbound_client(inbound_id, route_id, client)
            return response(request, {"success": True, "obj": True})
        if request.method == "POST" and "/resetClientTraffic/" in path:
            state["reset_calls"] += 1
            parts = path.split("/")
            inbound_id = int(parts[-3])
            route_id = parts[-1]
            for inbound in inbounds:
                if int(inbound["id"]) != inbound_id:
                    continue
                for row in inbound["clientStats"]:
                    if str(row.get("email")) == route_id or any(
                        str(client.get("email")) == str(row.get("email"))
                        and route_id
                        in {
                            str(client.get("id") or ""),
                            str(client.get("password") or ""),
                            str(client.get("email") or ""),
                        }
                        for client in json.loads(inbound["settings"])["clients"]
                    ):
                        row["up"] = 0
                        row["down"] = 0
            return response(request, {"success": True, "obj": True})
        if request.method == "POST" and path.endswith("/base/xui/API/inbounds/onlines"):
            emails = []
            for inbound in inbounds:
                emails.extend(
                    str(client.get("email"))
                    for client in json.loads(inbound["settings"])["clients"]
                )
            return response(request, {"success": True, "obj": emails[:1]})
        if request.method == "POST" and path.endswith("/base/xui/API/inbounds/lastOnline"):
            result = {}
            for inbound in inbounds:
                for client in json.loads(inbound["settings"])["clients"]:
                    result[str(client.get("email"))] = 1790633400000
            return response(request, {"success": True, "obj": result})
        if request.method == "POST" and "/delClient/" in path:
            parts = path.split("/")
            inbound_id = int(parts[-3])
            route_id = parts[-1]
            for inbound in inbounds:
                if int(inbound["id"]) != inbound_id:
                    continue
                settings = json.loads(inbound["settings"])
                kept = []
                for client in settings["clients"]:
                    values = {
                        str(client.get("id") or ""),
                        str(client.get("password") or ""),
                        str(client.get("email") or ""),
                    }
                    if route_id not in values:
                        kept.append(client)
                settings["clients"] = kept
                inbound["settings"] = json.dumps(settings)
                inbound["clientStats"] = [
                    row
                    for row in inbound["clientStats"]
                    if str(row.get("email"))
                    in {str(x.get("email")) for x in kept}
                ]
            return response(request, {"success": True, "obj": True})
        return httpx.Response(404, request=request)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    secret = json.dumps(
        {
            "version": 1,
            "flavor": "alireza",
            "username": "admin",
            "password": "pass",
            "secret_header": "header-secret",
        }
    )
    request = _request()
    created = adapter.provision(
        target=_alireza_target(),
        secret=secret,
        request=request,
    )
    assert state["add_calls"] == 2
    assert state["header_seen"] is True
    assert created.subscription_url == (
        f"https://sub-alireza.example/sub/{created.external_ref}"
    )
    clients = [
        client
        for inbound in inbounds
        for client in json.loads(inbound["settings"])["clients"]
    ]
    assert len(clients) == 2
    assert {str(x.get("subId")) for x in clients} == {created.external_ref}
    assert len({str(x.get("email")) for x in clients}) == 2

    adapter.provision(
        target=_alireza_target(),
        secret=secret,
        request=request,
    )
    assert state["add_calls"] == 2

    # Aggregate traffic across both inbound copies.
    inbounds[0]["clientStats"][0]["up"] = 1024
    inbounds[1]["clientStats"][0]["down"] = 2048
    snapshot = adapter.get_user(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
    )
    assert snapshot.usage_bytes == 3072
    assert snapshot.active is True
    assert snapshot.last_online

    inventory=adapter.list_users(target=_alireza_target(),secret=secret)
    assert len(inventory)==1 and inventory[0]['external_ref']==created.external_ref
    adapter.update_user(target=_alireza_target(),secret=secret,external_ref=created.external_ref,
                        changes={'name':'Legacy alias','traffic_bytes':25*1024**3})
    current=adapter.get_user(target=_alireza_target(),secret=secret,external_ref=created.external_ref)
    assert current.usage_bytes==snapshot.usage_bytes and state['reset_calls']==0

    renewal = RenewRequest(
        traffic_bytes=40 * 1024**3,
        duration_days=30,
        expires_at=iso_utc(utcnow() + timedelta(days=30)),
        idempotency_key="tenant:7:renewal-order:99",
    )
    renewed = adapter.renew(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
        request=renewal,
    )
    assert renewed.traffic_bytes == 40 * 1024**3
    assert state["reset_calls"] == 2

    # Both inbound copies have the retry marker, so later traffic survives retry.
    inbounds[0]["clientStats"][0]["up"] = 3 * 1024**3
    inbounds[1]["clientStats"][0]["down"] = 2 * 1024**3
    retry = adapter.renew(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
        request=renewal,
    )
    assert state["reset_calls"] == 2
    assert retry.usage_bytes == 5 * 1024**3

    disabled = adapter.set_enabled(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
        enabled=False,
    )
    assert disabled.active is False
    enabled = adapter.set_enabled(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
        enabled=True,
    )
    assert enabled.active is True

    adapter.delete_user(
        target=_alireza_target(),
        secret=secret,
        external_ref=created.external_ref,
    )
    assert all(
        json.loads(inbound["settings"])["clients"] == []
        for inbound in inbounds
    )


def test_alireza_partial_multi_inbound_retry_fills_only_missing_inbound() -> None:
    inbounds = [
        {
            "id": 1,
            "protocol": "vless",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
        {
            "id": 2,
            "protocol": "vless",
            "enable": True,
            "settings": json.dumps({"clients": []}),
            "clientStats": [],
        },
    ]
    state = {"adds": []}

    def response(request: httpx.Request, payload) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        if path.endswith("/login"):
            return response(request, {"success": True})
        if request.method == "GET" and path.endswith("/xui/API/inbounds/"):
            return response(request, {"success": True, "obj": inbounds})
        if request.method == "POST" and path.endswith("/xui/API/inbounds/addClient"):
            inbound_id = int(body["id"])
            state["adds"].append(inbound_id)
            client = json.loads(body["settings"])["clients"][0]
            for inbound in inbounds:
                if int(inbound["id"]) == inbound_id:
                    inbound["settings"] = json.dumps({"clients": [client]})
                    inbound["clientStats"] = [
                        {"email": client["email"], "up": 0, "down": 0}
                    ]
            return response(request, {"success": True})
        if request.method == "POST" and path.endswith("/xui/API/inbounds/onlines"):
            return response(request, {"success": True, "obj": []})
        if request.method == "POST" and path.endswith("/xui/API/inbounds/lastOnline"):
            return response(request, {"success": True, "obj": {}})
        return httpx.Response(404, request=request)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        kind="xui",
        endpoint="https://legacy.example",
        xui_flavor="alireza",
        xui_inbound_ids="1,2",
    )
    secret = json.dumps(
        {
            "flavor": "alireza",
            "username": "admin",
            "password": "pass",
        }
    )
    request = _request(key="partial-retry")
    stable_ref = str(
        __import__("uuid").uuid5(
            __import__("uuid").UUID("64e44dc9-ef21-4c43-bb64-2545f44c93bd"),
            "whitelabel:partial-retry",
        )
    )
    # Simulate an earlier crash after inbound 1 was already created.
    first = {
        "email": "wl-t7-s23",
        "id": stable_ref,
        "subId": stable_ref,
        "totalGB": 20 * 1024**3,
        "expiryTime": 0,
        "enable": True,
        "comment": "partial",
    }
    inbounds[0]["settings"] = json.dumps({"clients": [first]})
    inbounds[0]["clientStats"] = [{"email": first["email"], "up": 0, "down": 0}]

    result = adapter.provision(target=target, secret=secret, request=request)
    assert result.external_ref == stable_ref
    assert state["adds"] == [2]
    assert all(
        any(
            str(client.get("subId")) == stable_ref
            for client in json.loads(inbound["settings"])["clients"]
        )
        for inbound in inbounds
    )


def test_xui_credentials_are_encrypted_and_xui_can_be_default_server(
    conn, factories, cipher
) -> None:
    owner = 7001
    tenant = factories.tenant(owner_telegram_id=owner)
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    server = service.add_server(
        owner,
        label="XUI-DE",
        panel_kind="xui",
        endpoint="https://panel.example",
        xui_flavor="alireza",
        xui_inbound_ids="0",
        xui_public_origin="https://sub.example",
        xui_sub_path="/sub/",
    )
    service.set_xui_credential(
        owner,
        server_id=int(server["id"]),
        username="admin-user",
        password="top-secret-pass",
        secret_header="app-secret",
    )
    dump = "\n".join(conn.iterdump())
    assert "top-secret-pass" not in dump
    assert "app-secret" not in dump
    assert "admin-user" not in dump
    chosen = service.set_default_server(owner, server_id=int(server["id"]))
    assert chosen["is_default"] == 1
    target = service._panel_target(chosen)
    assert target.xui_flavor == "alireza"
    assert target.xui_inbound_ids == "0"
    assert target.xui_public_origin == "https://sub.example"


def test_invalid_xui_target_never_echoes_secret() -> None:
    adapter = XuiPanelAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )
    secret = json.dumps(
        {
            "flavor": "alireza",
            "username": "admin",
            "password": "do-not-leak",
        }
    )
    with pytest.raises(PanelError) as exc:
        adapter.get_user(
            target=PanelTarget(
                kind="xui",
                endpoint="file:///etc/passwd",
                xui_flavor="alireza",
            ),
            secret=secret,
            external_ref="user-id",
        )
    assert "do-not-leak" not in str(exc.value)

def test_xui_native_subscription_content_is_fetched() -> None:
    body = "trojan://pass@xui.example:443#XUI"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sub/u-content"
        return httpx.Response(200, request=request, text=body)

    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(
        kind="xui",
        endpoint="https://panel.example",
        xui_flavor="sanaei",
        xui_public_origin="https://sub.example",
        xui_sub_path="/sub/",
    )
    assert adapter.subscription_content(
        target=target, secret="unused", external_ref="u-content"
    ) == body

