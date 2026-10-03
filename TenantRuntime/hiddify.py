"""Independent Hiddify Manager live adapter for TenantRuntime.

This module intentionally contains no imports from Hiddify-SellBot.  Its API
dialect follows the Hiddify Manager v11/v12 endpoints and translates account
state fields for v13+.
"""

from __future__ import annotations

from dataclasses import asdict, replace

import hashlib
import math
import os
import re
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from TenantRuntime.panels import (
    PanelError,
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)

_GIB = 1024 ** 3
_UUID_NAMESPACE = uuid.UUID("ee6fb2f8-49c2-4de8-8d6e-a94da8867644")


class _StatusError(PanelError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"panel returned HTTP {int(status_code)}")
        self.status_code = int(status_code)


def _clean_base(endpoint: str) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise PanelError("invalid Hiddify endpoint")
    if parsed.query or parsed.fragment:
        raise PanelError("invalid Hiddify endpoint")
    return text


def _clean_path(value: str, *, name: str) -> str:
    text = str(value or "").strip().strip("/")
    if not text or any(part in {"", ".", ".."} for part in text.split("/")):
        raise PanelError(f"Hiddify {name} path is not configured")
    if "?" in text or "#" in text:
        raise PanelError(f"invalid Hiddify {name} path")
    return text


def _admin_base(target: PanelTarget) -> str:
    return f"{_clean_base(target.endpoint)}/{_clean_path(target.admin_path, name='admin')}"


def _user_base(target: PanelTarget, external_ref: str) -> str:
    ref = str(external_ref or "").strip()
    if not ref or "/" in ref or "?" in ref or "#" in ref:
        raise PanelError("invalid panel user reference")
    return f"{_clean_base(target.public_origin or target.endpoint)}/{_clean_path(target.user_path, name='user')}/{ref}"


def _api_timeout() -> float:
    try:
        value = float(os.getenv("HIDDIFY_API_TIMEOUT_SECONDS", "8") or "8")
    except (TypeError, ValueError):
        value = 8.0
    return min(max(value, 2.0), 60.0)


def _ssl_mode() -> str:
    raw = str(os.getenv("HIDDIFY_SSL_MODE", "secure") or "secure").strip().lower()
    aliases = {
        "1": "secure",
        "true": "secure",
        "on": "secure",
        "0": "insecure",
        "false": "insecure",
        "off": "insecure",
    }
    value = aliases.get(raw, raw)
    return value if value in {"secure", "insecure", "auto"} else "secure"


def _insecure_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _looks_like_tls_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(word in text for word in ("ssl", "tls", "certificate", "hostname", "cert"))


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "active", "enabled", "enable", "y"}:
        return True
    if text in {"0", "false", "no", "off", "inactive", "disabled", "disable", "n"}:
        return False
    return None


def _is_disabled(data: dict[str, Any]) -> bool:
    mode = str(data.get("mode") or "").strip().lower()
    status = str(data.get("status") or "").strip().lower()
    if mode in {"disable", "disabled", "inactive"}:
        return True
    if status in {"disable", "disabled", "inactive", "deactive", "off"}:
        return True
    return any(
        _coerce_bool(data.get(key)) is False
        for key in ("is_active", "active", "enabled", "enable")
        if key in data
    )


def _is_enabled(data: dict[str, Any]) -> bool:
    if _is_disabled(data):
        return False
    mode = str(data.get("mode") or "").strip().lower()
    status = str(data.get("status") or "").strip().lower()
    if mode in {"no_reset", "active", "enabled", "enable"}:
        return True
    if status in {"active", "enabled", "enable", "on"}:
        return True
    return any(
        _coerce_bool(data.get(key)) is True
        for key in ("is_active", "active", "enabled", "enable")
        if key in data
    )


def _gb_to_bytes(value: Any) -> int:
    try:
        return max(0, int(round(float(value or 0) * _GIB)))
    except (TypeError, ValueError):
        return 0


def _bytes_to_gb(value: int) -> float:
    return round(max(0, int(value)) / _GIB, 6)


def _parse_utcish_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_major(version: Any) -> int:
    match = re.match(r"^\s*v?(\d+)", str(version or "").strip(), flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _normalize_state_payload(payload: dict[str, Any], panel_major: int) -> dict[str, Any]:
    result = dict(payload)
    if int(panel_major) < 13:
        return result
    requested: bool | None = None
    for key in ("enable", "is_active", "enabled", "active"):
        if key in result:
            parsed = _coerce_bool(result.get(key))
            if parsed is not None:
                requested = parsed
                break
    mode = str(result.get("mode") or "").strip().lower()
    if mode in {"disable", "disabled", "inactive"}:
        requested = False
        result["mode"] = "no_reset"
    for key in ("is_active", "enabled", "active", "status"):
        result.pop(key, None)
    if requested is not None:
        result["enable"] = requested
    return result


class HiddifyPanelAdapter:
    """Synchronous Hiddify v11/v12/v13 adapter used behind the runtime boundary."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = float(timeout_seconds if timeout_seconds is not None else _api_timeout())
        self._version_cache: dict[tuple[str, str], int] = {}

    def _send(
        self,
        method: str,
        url: str,
        secret: str,
        *,
        payload: dict[str, Any] | None = None,
        verify: bool | ssl.SSLContext = True,
    ) -> httpx.Response:
        headers = {"Accept": "application/json", "Hiddify-API-Key": str(secret)}
        with httpx.Client(
            timeout=self._timeout,
            verify=verify,
            transport=self._transport,
        ) as client:
            return client.request(method, url, headers=headers, json=payload)

    def _request(
        self,
        method: str,
        url: str,
        secret: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        mode = _ssl_mode()
        try:
            if mode == "insecure":
                response = self._send(method, url, secret, payload=payload, verify=_insecure_context())
            elif mode == "auto":
                try:
                    response = self._send(method, url, secret, payload=payload, verify=True)
                except httpx.TransportError as exc:
                    if not _looks_like_tls_error(exc):
                        raise
                    response = self._send(
                        method, url, secret, payload=payload, verify=_insecure_context()
                    )
            else:
                response = self._send(method, url, secret, payload=payload, verify=True)
        except httpx.TransportError as exc:
            raise PanelError("Hiddify connection failed") from exc

        if response.status_code >= 400:
            raise _StatusError(response.status_code)
        if not response.content:
            return None
        content_type = str(response.headers.get("content-type") or "").lower()
        if "json" in content_type:
            try:
                return response.json()
            except ValueError as exc:
                raise PanelError("Hiddify returned invalid JSON") from exc
        try:
            return response.json()
        except ValueError:
            return response.text

    def _major(self, target: PanelTarget, secret: str) -> int:
        key = (_clean_base(target.endpoint), str(target.admin_path or "").strip("/"))
        if key in self._version_cache:
            return self._version_cache[key]
        major = 0
        try:
            data = self._request(
                "GET", f"{_admin_base(target)}/api/v2/panel/info/", secret
            )
            if isinstance(data, dict):
                major = _parse_major(data.get("version"))
        except PanelError:
            major = 0
        self._version_cache[key] = major
        return major

    def _user_url(self, target: PanelTarget, external_ref: str) -> str:
        return f"{_admin_base(target)}/api/v2/admin/user/{str(external_ref).strip()}/"

    def _patch(
        self,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = _normalize_state_payload(payload, self._major(target, secret))
        data = self._request(
            "PATCH", self._user_url(target, external_ref), secret, payload=body
        )
        if not isinstance(data, dict):
            raise PanelError("Hiddify returned an invalid user response")
        return data

    def _get(self, target: PanelTarget, secret: str, external_ref: str) -> dict[str, Any]:
        data = self._request("GET", self._user_url(target, external_ref), secret)
        if not isinstance(data, dict):
            raise PanelError("Hiddify returned an invalid user response")
        return data

    def _set_enabled_raw(
        self, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> dict[str, Any]:
        attempts = (
            ({"is_active": True}, {"enable": True}, {"status": "active"}, {"mode": "no_reset"})
            if enabled
            else (
                {"is_active": False},
                {"enable": False},
                {"status": "disable"},
                {"mode": "disable"},
            )
        )
        last: dict[str, Any] | None = None
        for payload in attempts:
            try:
                last = self._patch(target, secret, external_ref, payload)
                current = self._get(target, secret, external_ref)
            except PanelError:
                continue
            if enabled and _is_enabled(current):
                return current
            if not enabled and _is_disabled(current):
                return current
            last = current
        if isinstance(last, dict):
            if enabled and _is_enabled(last):
                return last
            if not enabled and _is_disabled(last):
                return last
        raise PanelError("Hiddify account state change could not be verified")

    def _snapshot(
        self, target: PanelTarget, external_ref: str, data: dict[str, Any]
    ) -> PanelUserResult:
        ref = str(data.get("uuid") or data.get("id") or external_ref or "").strip()
        if not ref:
            raise PanelError("Hiddify user response has no identifier")
        expires = None
        for key in (
            "expire",
            "expire_date",
            "end_date",
            "expires_at",
            "expiry_date",
            "expiration_date",
        ):
            if data.get(key) not in (None, ""):
                expires = str(data.get(key))
                break
        if expires is None:
            start = _parse_utcish_datetime(data.get("start_date"))
            days = int(data.get("package_days") or 0)
            if start is not None and days > 0:
                expires = (start + timedelta(days=days)).isoformat().replace("+00:00", "Z")
        last_online = (
            str(data.get("last_online")).strip()
            if data.get("last_online") not in (None, "")
            else None
        )
        return PanelUserResult(
            external_ref=ref,
            usage_bytes=_gb_to_bytes(data.get("current_usage_GB")),
            active=_is_enabled(data) and not _is_disabled(data),
            traffic_bytes=_gb_to_bytes(data.get("usage_limit_GB")),
            expires_at=expires,
            last_online=last_online,
            subscription_url=self.subscription_link(target=target, external_ref=ref),
            name=str(data.get("name") or ref), comment=str(data.get("comment") or ""),
        )

    def list_users(self, *, target: PanelTarget, secret: str) -> list[dict]:
        data = self._request("GET", f"{_admin_base(target)}/api/v2/admin/user/", secret)
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise PanelError("Hiddify returned an invalid user list")
        return [asdict(self._snapshot(target, str(x.get("uuid") or x.get("id") or ""), x))
                | {"duration_days": x.get("package_days"), "start_date": x.get("start_date")}
                for x in data]

    def update_user(self, *, target: PanelTarget, secret: str,
                    external_ref: str, changes: dict) -> PanelUserResult:
        current = self._get(target, secret, external_ref)
        payload = {key: changes[key] for key in ("name", "comment") if key in changes}
        if "traffic_bytes" in changes:
            payload["usage_limit_GB"] = _bytes_to_gb(changes["traffic_bytes"])
        if "duration_days" in changes:
            payload["package_days"] = int(changes["duration_days"])
        if "expires_at" in changes:
            expiry = _parse_utcish_datetime(changes["expires_at"])
            start = _parse_utcish_datetime(current.get("start_date"))
            if expiry is None:
                raise PanelError("invalid Hiddify expiry")
            if changes.get("reset_days") or start is None:
                start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                payload["start_date"] = start.strftime("%Y-%m-%d")
            payload["package_days"] = max(1, int(math.ceil((expiry-start).total_seconds()/86400)))
        if "start_date" in changes:
            payload["start_date"] = changes["start_date"]
        if changes.get("reset_usage"):
            payload["current_usage_GB"] = 0
            payload["last_reset_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._patch(target, secret, external_ref, payload)
        return self.get_user(target=target, secret=secret, external_ref=external_ref)

    def server_stats(self, *, target: PanelTarget, secret: str) -> dict:
        """SellBot-compatible Hiddify system and traffic statistics."""
        if str(target.kind or "").strip().lower() != "hiddify":
            raise PanelError("Hiddify adapter received the wrong panel kind")

        def _number(value: Any, default: float = 0.0) -> float:
            try:
                return float(value if value is not None else default)
            except (TypeError, ValueError):
                return float(default)

        def _integer(value: Any, default: int = 0) -> int:
            try:
                return int(float(value if value is not None else default))
            except (TypeError, ValueError):
                return int(default)

        def _bytes_gb(value: Any) -> float:
            return max(0.0, _number(value, 0.0)) / float(1024 ** 3)

        try:
            users = self.list_users(target=target, secret=secret)
        except PanelError:
            users = []

        now = datetime.now(timezone.utc)
        total_usage = 0.0
        online_now = 0
        active_today = 0
        active_month = 0
        for user in users:
            if not isinstance(user, dict):
                continue
            total_usage += max(0, int(user.get("usage_bytes") or 0)) / float(1024 ** 3)
            seen = _parse_utcish_datetime(user.get("last_online"))
            if seen is None:
                continue
            age = max(0.0, (now - seen).total_seconds())
            if age < 300:
                online_now += 1
            if age < 86400:
                active_today += 1
            if age < 30 * 86400:
                active_month += 1

        out: dict[str, Any] = {
            "cpu_percent": 0.0,
            "cpu_cores": 1,
            "ram_used": 0.0,
            "ram_total": 1.0,
            "disk_used": 0.0,
            "disk_total": 20.0,
            "users_total": len(users),
            "users_online": online_now,
            "users_today": active_today,
            "users_month": active_month,
            "usage_today_gb": 0.0,
            "usage_30days_gb": total_usage,
            "traffic_dl": 0.0,
            "traffic_ul": 0.0,
            "now_net_recv_mb": 0.0,
            "now_net_sent_mb": 0.0,
        }

        try:
            data = self._request(
                "GET",
                f"{_admin_base(target)}/api/v2/admin/server_status/",
                secret,
            )
        except PanelError:
            return out
        if not isinstance(data, dict):
            return out

        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        history = (
            data.get("usage_history")
            if isinstance(data.get("usage_history"), dict)
            else {}
        )
        system = stats.get("system") if isinstance(stats.get("system"), dict) else {}

        out["cpu_percent"] = _number(system.get("cpu_percent"), out["cpu_percent"])
        out["cpu_cores"] = _integer(system.get("num_cpus"), out["cpu_cores"])
        out["ram_used"] = _number(system.get("ram_used"), out["ram_used"])
        out["ram_total"] = max(0.0001, _number(system.get("ram_total"), out["ram_total"]))
        out["disk_used"] = _number(system.get("disk_used"), out["disk_used"])
        out["disk_total"] = max(0.0001, _number(system.get("disk_total"), out["disk_total"]))
        out["now_net_recv_mb"] = _number(system.get("bytes_recv"), 0.0) / float(1024 ** 2)
        out["now_net_sent_mb"] = _number(system.get("bytes_sent"), 0.0) / float(1024 ** 2)

        total_block = history.get("total") if isinstance(history.get("total"), dict) else {}
        today_block = history.get("today") if isinstance(history.get("today"), dict) else {}
        month_block = (
            history.get("last_30_days")
            if isinstance(history.get("last_30_days"), dict)
            else {}
        )
        m5_block = history.get("m5") if isinstance(history.get("m5"), dict) else {}

        out["users_total"] = _integer(total_block.get("users"), out["users_total"])
        out["users_online"] = _integer(m5_block.get("online"), out["users_online"])
        out["users_today"] = _integer(today_block.get("online"), out["users_today"])
        out["users_month"] = _integer(month_block.get("online"), out["users_month"])
        out["usage_today_gb"] = _bytes_gb(today_block.get("usage"))
        month_usage = _bytes_gb(month_block.get("usage"))
        if month_usage > 0:
            out["usage_30days_gb"] = month_usage

        sent_gb = _number(system.get("net_sent_cumulative_GB"), 0.0)
        recv_gb = _bytes_gb(system.get("bytes_recv_cumulative"))
        out["traffic_ul"] = sent_gb
        out["traffic_dl"] = recv_gb
        total_gb = _number(system.get("net_total_cumulative_GB"), 0.0)
        if recv_gb <= 0 and total_gb > 0:
            out["traffic_dl"] = max(0.0, total_gb - sent_gb)
        return out

    def inspect_connection(self, *, target: PanelTarget, secret: str) -> dict:
        if target.kind != "hiddify":
            raise PanelError("Hiddify adapter received the wrong panel kind")
        _clean_path(target.user_path, name="user")
        # SellBot tests the protected users API, not the public panel homepage.
        users = self._request("GET", f"{_admin_base(target)}/api/v2/admin/user/", secret)
        if not isinstance(users, list) or any(not isinstance(x, dict) for x in users):
            raise PanelError("Hiddify returned an invalid user list")
        return {"connected": True, "users_count": len(users),
                "panel_major": self._major(target, secret), "inbounds": []}

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        if str(target.kind).strip().lower() != "hiddify":
            raise PanelError("Hiddify adapter received the wrong panel kind")
        external_ref = request.external_ref or str(
            uuid.uuid5(_UUID_NAMESPACE, f"whitelabel:{request.idempotency_key}")
        )
        payload = {
            "uuid": external_ref,
            "name": request.name or f"wl-t{int(request.tenant_id)}-s{int(request.subscription_id)}",
            "usage_limit_GB": _bytes_to_gb(request.traffic_bytes),
            "package_days": max(1, int(request.duration_days)),
            "start_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "current_usage_GB": 0,
            "is_active": True,
            "comment": (
                f"WhiteLabel tenant={int(request.tenant_id)} "
                f"subscription={int(request.subscription_id)}"
            ),
        }
        body = _normalize_state_payload(payload, self._major(target, secret))
        try:
            data = self._request(
                "POST", f"{_admin_base(target)}/api/v2/admin/user/", secret, payload=body
            )
        except _StatusError as exc:
            if exc.status_code not in {400, 409, 422}:
                raise
            # Deterministic UUID makes a retry safe: accept an already-created row.
            try:
                data = self._get(target, secret, external_ref)
            except PanelError:
                raise exc
        if not isinstance(data, dict):
            raise PanelError("Hiddify returned an invalid create response")
        created_ref = str(data.get("uuid") or external_ref).strip()
        if not created_ref:
            raise PanelError("Hiddify create response has no identifier")
        # Cross-version activation verification; this mirrors the proven
        # compatibility behavior without depending on SellBot.
        self._set_enabled_raw(target, secret, created_ref, True)
        return ProvisionResult(
            external_ref=created_ref,
            subscription_url=self.subscription_link(
                target=target, external_ref=created_ref
            ),
        )

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        return self._snapshot(target, external_ref, self._get(target, secret, external_ref))

    def renew(
        self, *, target: PanelTarget, secret: str, external_ref: str, request: RenewRequest
    ) -> PanelUserResult:
        current = self._get(target, secret, external_ref)
        marker = ""
        if str(request.idempotency_key or "").strip():
            digest = hashlib.sha256(
                str(request.idempotency_key).encode("utf-8")
            ).hexdigest()[:20]
            marker = f"wl-renew:{digest}"
            if marker in str(current.get("comment") or ""):
                # The remote reset already happened in an earlier attempt. Only
                # verify/re-enable; never zero usage twice on a retry.
                enabled = self._set_enabled_raw(target, secret, external_ref, True)
                return self._snapshot(target, external_ref, enabled)

        package_days = max(1, int(request.duration_days))
        if not bool(request.reset_time):
            target_expiry = _parse_utcish_datetime(request.expires_at)
            current_start = _parse_utcish_datetime(current.get("start_date"))
            if target_expiry is None or current_start is None:
                raise PanelError(
                    "Hiddify renewal cannot preserve time without start date"
                )
            seconds = (target_expiry - current_start).total_seconds()
            if seconds <= 0:
                raise PanelError("Hiddify renewal target expiry is invalid")
            package_days = max(1, int(math.ceil(seconds / 86400.0)))

        payload: dict[str, Any] = {
            "usage_limit_GB": _bytes_to_gb(request.traffic_bytes),
            "package_days": package_days,
        }
        if request.reset_usage:
            payload["current_usage_GB"] = 0
        if request.reset_time:
            payload["start_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if marker:
            comment = str(current.get("comment") or "").strip()
            payload["comment"] = f"{comment} {marker}".strip()[:500]
        self._patch(target, secret, external_ref, payload)
        current = self._set_enabled_raw(target, secret, external_ref, True)
        return self._snapshot(target, external_ref, current)

    def set_enabled(
        self, *, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> PanelUserResult:
        current = self._set_enabled_raw(target, secret, external_ref, bool(enabled))
        return self._snapshot(target, external_ref, current)

    def validate_identity_rotation(self, *, target: PanelTarget, secret: str,
                                   external_ref: str) -> None:
        self._get(target, secret, external_ref)

    def rotate_identity(self, *, target: PanelTarget, secret: str,
                        external_ref: str, new_ref: str) -> PanelUserResult:
        # Retry the same desired UUID after a lost response, without re-creating
        # a user or touching usage/quota/expiry fields.
        try:
            self._get(target, secret, external_ref)
        except PanelError:
            data = self._get(target, secret, new_ref)
        else:
            self._patch(target, secret, external_ref, {"uuid": new_ref})
            data = self._get(target, secret, new_ref)
        try:
            self._get(target, secret, external_ref)
        except PanelError:
            pass
        else:
            raise PanelError("Hiddify old credentials are still present")
        result = self._snapshot(target, new_ref, data)
        if result.external_ref != new_ref:
            raise PanelError("Hiddify credential rotation could not be verified")
        return result

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        try:
            self._request("DELETE", self._user_url(target, external_ref), secret)
            return
        except _StatusError as exc:
            if exc.status_code == 404:
                return
        # Hiddify versions without DELETE fall back to a verified disable.
        self._set_enabled_raw(target, secret, external_ref, False)

    def _refresh_usage(self, target: PanelTarget, secret: str) -> None:
        try:
            data = self._request(
                "GET", f"{_admin_base(target)}/api/v2/admin/update_user_usage/", secret
            )
            # v11/v12 may JSON-encode the object twice.  Nothing in this
            # response is trusted for the per-user counter; the following GET is.
            if isinstance(data, str):
                data.strip()
        except PanelError:
            return

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        self._refresh_usage(target, secret)
        user = self._snapshot(target, external_ref, self._get(target, secret, external_ref))
        return UsageResult(
            usage_bytes=user.usage_bytes,
            active=user.active,
            last_online=user.last_online,
        )

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        return f"{_user_base(target, external_ref)}/all.txt"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        data = self._request(
            "GET",
            self.subscription_link(target=replace(target,public_origin=""), external_ref=external_ref),
            secret,
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        raise PanelError("Hiddify subscription is empty")
