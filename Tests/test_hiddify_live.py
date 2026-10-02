"""Hiddify live adapter: v11/v12/v13 compatibility and safe runtime routing."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.panels import (
    PanelError,
    PanelTarget,
    ProvisionRequest,
    RenewRequest,
)


def _target() -> PanelTarget:
    return PanelTarget(
        kind="hiddify",
        endpoint="https://panel.example",
        admin_path="admin-secret",
        user_path="user-secret",
    )


def _request() -> ProvisionRequest:
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


def _json_response(status: int, payload, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        status,
        request=request,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


def test_v13_create_translates_is_active_and_returns_native_subscription_link() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "13.0.0"}, request)
        if request.method == "POST":
            assert body["enable"] is True
            assert "is_active" not in body
            return _json_response(200, {"uuid": body["uuid"], "enable": True}, request)
        if request.method == "PATCH":
            return _json_response(200, {"uuid": request.url.path.split("/")[-2], "enable": True}, request)
        return _json_response(
            200,
            {
                "uuid": request.url.path.split("/")[-2],
                "enable": True,
                "current_usage_GB": 0,
                "usage_limit_GB": 20,
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    result = adapter.provision(target=_target(), secret="api-key", request=_request())
    assert result.external_ref
    assert result.subscription_url == (
        f"https://panel.example/user-secret/{result.external_ref}/all.txt"
    )
    assert any(path.endswith("/admin-secret/api/v2/admin/user/") for _, path, _ in seen)


def test_v12_renew_keeps_legacy_fields_and_resets_usage() -> None:
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "12.3.3"}, request)
        if request.method == "PATCH":
            patches.append(body)
            return _json_response(200, {"uuid": "u-1", "is_active": True}, request)
        return _json_response(
            200,
            {
                "uuid": "u-1",
                "is_active": True,
                "current_usage_GB": 0,
                "usage_limit_GB": 50,
                "last_online": "2026-09-28 21:00:00",
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    result = adapter.renew(
        target=_target(),
        secret="api-key",
        external_ref="u-1",
        request=RenewRequest(
            traffic_bytes=50 * 1024**3,
            duration_days=30,
            expires_at=iso_utc(utcnow() + timedelta(days=30)),
        ),
    )
    assert patches[0]["usage_limit_GB"] == 50.0
    assert patches[0]["package_days"] == 30
    assert patches[0]["current_usage_GB"] == 0
    assert "start_date" in patches[0]
    assert result.active is True


def test_renew_add_time_preserves_start_date_and_extends_package_days() -> None:
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "12.3.3"}, request)
        if request.method == "PATCH":
            patches.append(body or {})
            return _json_response(
                200,
                {
                    "uuid": "u-add-time",
                    "is_active": True,
                    "current_usage_GB": 7,
                    "usage_limit_GB": 80,
                    "start_date": "2026-09-01",
                },
                request,
            )
        return _json_response(
            200,
            {
                "uuid": "u-add-time",
                "is_active": True,
                "current_usage_GB": 7,
                "usage_limit_GB": 50,
                "start_date": "2026-09-01",
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    adapter.renew(
        target=_target(),
        secret="api-key",
        external_ref="u-add-time",
        request=RenewRequest(
            traffic_bytes=80 * 1024**3,
            duration_days=40,
            expires_at="2026-11-01T00:00:00+00:00",
            reset_usage=False,
            reset_time=False,
        ),
    )

    renewal_patch = next(p for p in patches if "package_days" in p)
    assert renewal_patch["package_days"] == 61
    assert renewal_patch["usage_limit_GB"] == 80.0
    assert "current_usage_GB" not in renewal_patch
    assert "start_date" not in renewal_patch


def test_renew_reset_time_is_independent_from_usage_reset() -> None:
    patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "12.3.3"}, request)
        if request.method == "PATCH":
            patches.append(body or {})
            return _json_response(
                200,
                {
                    "uuid": "u-reset-time",
                    "is_active": True,
                    "current_usage_GB": 7,
                    "usage_limit_GB": 80,
                    "start_date": "2026-10-03",
                },
                request,
            )
        return _json_response(
            200,
            {
                "uuid": "u-reset-time",
                "is_active": True,
                "current_usage_GB": 7,
                "usage_limit_GB": 50,
                "start_date": "2026-09-01",
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    adapter.renew(
        target=_target(),
        secret="api-key",
        external_ref="u-reset-time",
        request=RenewRequest(
            traffic_bytes=80 * 1024**3,
            duration_days=30,
            expires_at="2026-11-02T00:00:00+00:00",
            reset_usage=False,
            reset_time=True,
        ),
    )

    renewal_patch = next(p for p in patches if "package_days" in p)
    assert renewal_patch["package_days"] == 30
    assert "current_usage_GB" not in renewal_patch
    assert "start_date" in renewal_patch


def test_usage_refreshes_official_counter_and_preserves_last_online() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/update_user_usage/"):
            return _json_response(200, {"status": "success"}, request)
        return _json_response(
            200,
            {
                "uuid": "u-2",
                "is_active": True,
                "current_usage_GB": 1.5,
                "usage_limit_GB": 10,
                "last_online": "2026-09-29 00:15:00",
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    usage = adapter.usage(target=_target(), secret="api-key", external_ref="u-2")
    assert usage.usage_bytes == int(1.5 * 1024**3)
    assert usage.active is True
    assert usage.last_online == "2026-09-29 00:15:00"
    assert any(path.endswith("/api/v2/admin/update_user_usage/") for path in calls)


def test_delete_falls_back_to_verified_disable() -> None:
    state = {"enabled": True}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "12.3.3"}, request)
        if request.method == "DELETE":
            return _json_response(405, {"detail": "method not allowed"}, request)
        if request.method == "PATCH":
            if body.get("is_active") is False:
                state["enabled"] = False
            return _json_response(200, {"uuid": "u-3", "is_active": state["enabled"]}, request)
        return _json_response(200, {"uuid": "u-3", "is_active": state["enabled"]}, request)

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    adapter.delete_user(target=_target(), secret="api-key", external_ref="u-3")
    assert state["enabled"] is False


def test_invalid_hiddify_target_fails_without_echoing_secret() -> None:
    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(lambda request: _json_response(500, {}, request)))
    with pytest.raises(PanelError) as exc:
        adapter.get_user(
            target=PanelTarget(kind="hiddify", endpoint="file:///etc/passwd", admin_path="x", user_path="y"),
            secret="super-secret",
            external_ref="u",
        )
    assert "super-secret" not in str(exc.value)

def test_renew_retry_marker_prevents_second_usage_reset() -> None:
    state = {
        "comment": "WhiteLabel tenant=7 subscription=23",
        "usage": 3.0,
        "enabled": False,
    }
    reset_patches: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.url.path.endswith("/api/v2/panel/info/"):
            return _json_response(200, {"version": "12.3.3"}, request)
        if request.method == "PATCH":
            if body.get("current_usage_GB") == 0:
                reset_patches.append(body)
                state["usage"] = 0
                state["comment"] = body.get("comment", state["comment"])
            if body.get("is_active") is True:
                state["enabled"] = True
            return _json_response(
                200,
                {
                    "uuid": "u-retry",
                    "is_active": state["enabled"],
                    "current_usage_GB": state["usage"],
                    "usage_limit_GB": 50,
                    "comment": state["comment"],
                },
                request,
            )
        return _json_response(
            200,
            {
                "uuid": "u-retry",
                "is_active": state["enabled"],
                "current_usage_GB": state["usage"],
                "usage_limit_GB": 50,
                "comment": state["comment"],
            },
            request,
        )

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    request = RenewRequest(
        traffic_bytes=50 * 1024**3,
        duration_days=30,
        expires_at=iso_utc(utcnow() + timedelta(days=30)),
        idempotency_key="tenant:7:renewal-order:99",
    )
    first = adapter.renew(
        target=_target(), secret="api-key", external_ref="u-retry", request=request
    )
    assert first.active is True
    assert len(reset_patches) == 1
    assert "wl-renew:" in state["comment"]

    # Simulate real traffic after the first successful remote renewal but before
    # the local transaction completed. Retrying must not zero this traffic.
    state["usage"] = 4.25
    state["enabled"] = False
    second = adapter.renew(
        target=_target(), secret="api-key", external_ref="u-retry", request=request
    )
    assert len(reset_patches) == 1
    assert second.usage_bytes == int(4.25 * 1024**3)
    assert second.active is True

def test_hiddify_native_subscription_content_is_fetched() -> None:
    body = "vless://user@h.example:443?type=tcp#H"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/user-secret/u-content/all.txt")
        return httpx.Response(200, request=request, text=body)

    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    assert adapter.subscription_content(
        target=_target(), secret="api-key", external_ref="u-content"
    ) == body

