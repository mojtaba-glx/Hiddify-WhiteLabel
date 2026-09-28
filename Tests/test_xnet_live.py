"""Live X-NET adapter coverage with offline MockTransport."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import PanelError, PanelTarget, ProvisionRequest, RenewRequest
from TenantRuntime.xnet import XnetPanelAdapter


def _target() -> PanelTarget:
    return PanelTarget(
        kind="xnet",
        endpoint="https://xnet.example:8080",
        xnet_inbound_ids="in-a,in-b",
        xnet_public_origin="https://sub.example",
        xnet_sub_port=2443,
        xnet_sub_path="vpn",
    )


def _provision_request() -> ProvisionRequest:
    return ProvisionRequest(
        tenant_id=7,
        server_id=11,
        subscription_id=23,
        customer_id=31,
        traffic_bytes=20 * 1024**3,
        duration_days=30,
        expires_at=iso_utc(utcnow() + timedelta(days=30)),
        idempotency_key="tenant:7:subscription:23",
    )


def _json(request: httpx.Request, payload, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        request=request,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


def test_xnet_live_create_renew_toggle_usage_delete_and_link() -> None:
    inbounds = [
        {"id": "in-a", "enabled": True, "protocol": "VLESS", "clients": []},
        {"id": "in-b", "enabled": True, "protocol": "HYSTERIA2", "clients": []},
    ]
    state = {
        "create_calls": 0,
        "reset_calls": 0,
        "delete_calls": 0,
        "token_headers": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v1/ping"):
            return _json(request, {"status": "ok"})

        auth = request.headers.get("Authorization", "")
        state["token_headers"].append(auth)
        assert auth == "Bearer api-token"
        body = json.loads(request.content.decode()) if request.content else None

        if request.method == "GET" and path.endswith("/api/inbounds"):
            return _json(request, inbounds)

        if request.method == "POST" and "/api/inbounds/" in path and path.endswith("/clients"):
            state["create_calls"] += 1
            primary = path.split("/api/inbounds/", 1)[1].split("/", 1)[0]
            targets = [primary] + list(body.get("extraInboundIds") or [])
            for index, inbound_id in enumerate(targets, 1):
                inbound = next(row for row in inbounds if row["id"] == inbound_id)
                inbound["clients"].append(
                    {
                        "id": f"client-{index}",
                        "username": body["username"],
                        "uuid": body["uuid"],
                        "status": body["status"],
                        "trafficLimitBytes": body["trafficLimitBytes"],
                        "trafficUsedBytes": index * 1024,
                        "expireDate": body["expireDate"],
                        "remark": body["remark"],
                        "extraInboundIds": list(body.get("extraInboundIds") or []),
                        "activeSessions": 0,
                    }
                )
            return _json(request, {"success": True})

        if request.method == "GET" and path.endswith("/api/online-users"):
            return _json(
                request,
                {
                    "singbox": [
                        {"clientId": "client-1", "devices": 1}
                    ]
                },
            )

        if request.method == "PUT" and "/clients/" in path:
            parts = path.split("/api/inbounds/", 1)[1].split("/")
            inbound_id, client_id = parts[0], parts[2]
            inbound = next(row for row in inbounds if row["id"] == inbound_id)
            client = next(row for row in inbound["clients"] if row["id"] == client_id)
            client.update(body)
            return _json(request, {"success": True})

        if request.method == "POST" and path.endswith("/reset-traffic"):
            state["reset_calls"] += 1
            parts = path.split("/api/inbounds/", 1)[1].split("/")
            inbound_id, client_id = parts[0], parts[2]
            inbound = next(row for row in inbounds if row["id"] == inbound_id)
            client = next(row for row in inbound["clients"] if row["id"] == client_id)
            client["trafficUsedBytes"] = 0
            return _json(request, {"success": True})

        if request.method == "DELETE" and "/api/v1/subscribers/" in path:
            state["delete_calls"] += 1
            wanted = path.rsplit("/", 1)[-1]
            for inbound in inbounds:
                inbound["clients"] = [
                    row for row in inbound["clients"] if row.get("uuid") != wanted
                ]
            return _json(request, {"success": True})

        if request.method == "GET" and "/sessions" in path:
            return _json(request, {"sessions": []})

        return httpx.Response(404, request=request)

    adapter = XnetPanelAdapter(transport=httpx.MockTransport(handler))
    request = _provision_request()
    created = adapter.provision(target=_target(), secret="api-token", request=request)
    assert state["create_calls"] == 1
    assert created.subscription_url == (
        f"https://sub.example:2443/vpn/{created.external_ref}"
    )
    assert all(
        any(client["uuid"] == created.external_ref for client in inbound["clients"])
        for inbound in inbounds
    )

    # Deterministic UUID makes create retry converge.
    again = adapter.provision(target=_target(), secret="api-token", request=request)
    assert again.external_ref == created.external_ref
    assert state["create_calls"] == 1

    snapshot = adapter.get_user(
        target=_target(), secret="api-token", external_ref=created.external_ref
    )
    assert snapshot.usage_bytes == 3 * 1024
    assert snapshot.active is True
    assert snapshot.last_online
    assert snapshot.traffic_bytes == 20 * 1024**3

    renewal = RenewRequest(
        traffic_bytes=50 * 1024**3,
        duration_days=45,
        expires_at=iso_utc(utcnow() + timedelta(days=45)),
        idempotency_key="tenant:7:renewal-order:88",
    )
    renewed = adapter.renew(
        target=_target(),
        secret="api-token",
        external_ref=created.external_ref,
        request=renewal,
    )
    assert renewed.traffic_bytes == 50 * 1024**3
    assert state["reset_calls"] == 2
    assert all(
        "wl-renew:" in client["remark"]
        for inbound in inbounds
        for client in inbound["clients"]
    )

    # Post-renew traffic must survive an identical retry.
    inbounds[0]["clients"][0]["trafficUsedBytes"] = 3 * 1024**3
    inbounds[1]["clients"][0]["trafficUsedBytes"] = 2 * 1024**3
    retried = adapter.renew(
        target=_target(),
        secret="api-token",
        external_ref=created.external_ref,
        request=renewal,
    )
    assert state["reset_calls"] == 2
    assert retried.usage_bytes == 5 * 1024**3

    disabled = adapter.set_enabled(
        target=_target(),
        secret="api-token",
        external_ref=created.external_ref,
        enabled=False,
    )
    assert disabled.active is False
    enabled = adapter.set_enabled(
        target=_target(),
        secret="api-token",
        external_ref=created.external_ref,
        enabled=True,
    )
    assert enabled.active is True

    usage = adapter.usage(
        target=_target(), secret="api-token", external_ref=created.external_ref
    )
    assert usage.usage_bytes == 5 * 1024**3

    adapter.delete_user(
        target=_target(), secret="api-token", external_ref=created.external_ref
    )
    assert state["delete_calls"] == 1
    assert all(inbound["clients"] == [] for inbound in inbounds)
    assert all("api-token" not in str(value) for value in state["token_headers"])


def test_xnet_rejected_api_token_falls_back_to_login_jwt() -> None:
    state = {"login_calls": 0, "protected_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v1/ping"):
            return _json(request, {"status": "ok"})
        if path.endswith("/api/auth/login"):
            state["login_calls"] += 1
            body = json.loads(request.content.decode())
            assert body == {"username": "admin", "password": "fallback-pass"}
            return _json(request, {"token": "fresh-jwt", "requires2fa": False})
        if path.endswith("/api/inbounds"):
            state["protected_calls"] += 1
            auth = request.headers.get("Authorization")
            if auth == "Bearer rejected-api-token":
                return _json(request, {"error": "forbidden"}, status=401)
            assert auth == "Bearer fresh-jwt"
            return _json(
                request,
                [
                    {
                        "id": "in-a",
                        "enabled": True,
                        "protocol": "VLESS",
                        "clients": [
                            {
                                "id": "c-1",
                                "uuid": "user-uuid",
                                "status": "active",
                                "trafficLimitBytes": 10 * 1024**3,
                                "trafficUsedBytes": 1234,
                                "expireDate": iso_utc(utcnow() + timedelta(days=5)),
                            }
                        ],
                    }
                ],
            )
        if path.endswith("/api/online-users"):
            assert request.headers.get("Authorization") == "Bearer fresh-jwt"
            return _json(request, {"singbox": []})
        if "/sessions" in path:
            return _json(request, {"sessions": []})
        return httpx.Response(404, request=request)

    adapter = XnetPanelAdapter(transport=httpx.MockTransport(handler))
    secret = json.dumps(
        {
            "provider": "xnet",
            "api_token": "rejected-api-token",
            "username": "admin",
            "password": "fallback-pass",
        }
    )
    user = adapter.get_user(
        target=PanelTarget(kind="xnet", endpoint="https://xnet.example"),
        secret=secret,
        external_ref="user-uuid",
    )
    assert user.usage_bytes == 1234
    assert state["login_calls"] == 1
    assert state["protected_calls"] >= 2


def test_xnet_subscription_default_listener_and_path() -> None:
    adapter = XnetPanelAdapter()
    target = PanelTarget(
        kind="xnet",
        endpoint="https://xnet.example:8080",
    )
    assert (
        adapter.subscription_link(target=target, external_ref="abc")
        == "https://xnet.example:2096/sub/abc"
    )


def test_xnet_server_credentials_are_encrypted_and_can_be_default(
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
        label="XNET-FR",
        panel_kind="xnet",
        endpoint="https://xnet.example:8080",
        xnet_inbound_ids="in-a,in-b",
        xnet_public_origin="https://sub.example",
        xnet_sub_port=2096,
        xnet_sub_path="sub",
    )
    assert server["panel_kind"] == "xnet"
    assert server["legacy_panel_kind"] == "manual"
    service.set_xnet_credential(
        owner,
        server_id=int(server["id"]),
        api_token="persistent-token",
        username="admin",
        password="secret-pass",
    )
    dump = "\n".join(conn.iterdump())
    assert "persistent-token" not in dump
    assert "secret-pass" not in dump
    chosen = service.set_default_server(owner, server_id=int(server["id"]))
    assert chosen["is_default"] == 1
    target = service._panel_target(chosen)
    assert target.kind == "xnet"
    assert target.xnet_inbound_ids == "in-a,in-b"
    assert target.xnet_sub_port == 2096


def test_xnet_provider_is_strictly_tenant_scoped(conn, factories, cipher) -> None:
    first = factories.tenant(owner_telegram_id=7001)
    second = factories.tenant(owner_telegram_id=7002)
    left = TenantBusinessService(
        conn,
        tenant_id=int(first["id"]),
        owner_telegram_id=7001,
        secret_cipher=cipher,
    )
    right = TenantBusinessService(
        conn,
        tenant_id=int(second["id"]),
        owner_telegram_id=7002,
        secret_cipher=cipher,
    )
    server = left.add_server(
        7001,
        label="XNET",
        panel_kind="xnet",
        endpoint="https://xnet.example",
    )
    left.set_xnet_credential(
        7001,
        server_id=int(server["id"]),
        api_token="tenant-one-token",
    )
    with pytest.raises(Exception):
        right.panel_status(int(server["id"]))


def test_invalid_xnet_target_never_echoes_secret() -> None:
    adapter = XnetPanelAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )
    secret = json.dumps(
        {
            "api_token": "do-not-leak",
            "username": "admin",
            "password": "also-secret",
        }
    )
    with pytest.raises(PanelError) as exc:
        adapter.get_user(
            target=PanelTarget(kind="xnet", endpoint="file:///etc/passwd"),
            secret=secret,
            external_ref="user-id",
        )
    assert "do-not-leak" not in str(exc.value)
    assert "also-secret" not in str(exc.value)
