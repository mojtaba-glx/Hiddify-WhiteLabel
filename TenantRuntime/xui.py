"""Independent X-UI live adapter for Sanaei 3x-ui and Alireza x-ui.

No runtime imports from Hiddify-SellBot.  The adapter implements the shared
PanelAdapter contract and keeps the two X-UI dialects behind one tenant-owned
routing target:
- Sanaei: /panel/api + Bearer API token.
- Alireza: /login cookie auth + /xui/API, with optional Xray secret header.
"""

from __future__ import annotations

from TenantRuntime.panels import decode_panel_note

from dataclasses import asdict

import hashlib
import json
import os
import ssl
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

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
_SUPPORTED_PROTOCOLS = {
    "vless",
    "vmess",
    "trojan",
    "shadowsocks",
    "hysteria",
    "hysteria2",
}
_UUID_NAMESPACE = uuid.UUID("64e44dc9-ef21-4c43-bb64-2545f44c93bd")
_SECRET_HEADER = "XUI-Xray-App-Secret-Key"


class _StatusError(PanelError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"X-UI returned HTTP {int(status_code)}")
        self.status_code = int(status_code)


def _timeout() -> float:
    raw = str(os.getenv("XUI_API_TIMEOUT_SECONDS", "12") or "").strip()
    try:
        return max(3.0, min(float(raw), 60.0))
    except ValueError:
        return 12.0


def _ssl_mode() -> str:
    mode = str(os.getenv("XUI_SSL_MODE", "secure") or "").strip().lower()
    return mode if mode in {"secure", "insecure", "auto"} else "secure"


def _insecure_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _looks_like_tls_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "certificate",
            "ssl",
            "tls",
            "hostname",
            "unknown ca",
            "self signed",
        )
    )


def _clean_base(endpoint: str) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PanelError("invalid X-UI endpoint")
    return text


def _clean_origin(value: str, fallback_endpoint: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        parsed = urlsplit(_clean_base(fallback_endpoint))
        host = parsed.hostname or ""
        port = parsed.port
        default_port = (parsed.scheme == "https" and port == 443) or (
            parsed.scheme == "http" and port == 80
        )
        netloc = host if not port or default_port else f"{host}:{port}"
        return f"{parsed.scheme}://{netloc}"
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PanelError("invalid X-UI public origin")
    return text


def _sub_path(target: PanelTarget) -> str:
    raw = str(target.xui_sub_path or "/sub/").strip() or "/sub/"
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.endswith("/"):
        raw += "/"
    if ".." in raw:
        raise PanelError("invalid X-UI subscription path")
    return raw


def _expiry_ms(raw: str) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PanelError("invalid subscription expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


def _expiry_iso(value: object) -> str | None:
    try:
        ms = int(float(value or 0))
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _timestamp_string(value: object) -> str | None:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    seconds = number / 1000 if number > 1_000_000_000_000 else number
    if seconds <= 1_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OSError, OverflowError, ValueError):
        return None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value if value is not None else default))
    except (TypeError, ValueError):
        return int(default)


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    parsed = _json_dict(inbound.get("settings"))
    value = parsed.get("clients")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _client_identity(client: dict[str, Any], protocol: str) -> str:
    proto = str(protocol or "").strip().lower()
    if proto in {"trojan", "hysteria", "hysteria2"}:
        return str(client.get("password") or client.get("auth") or client.get("email") or "").strip()
    if proto == "shadowsocks":
        return str(client.get("email") or client.get("password") or "").strip()
    return str(client.get("id") or client.get("email") or "").strip()


def _matches(client: dict[str, Any], external_ref: str) -> bool:
    needle = str(external_ref or "").strip().lower()
    if not needle:
        return False
    values = (
        client.get("subId"),
        client.get("uuid"),
        client.get("id"),
        client.get("password"),
        client.get("auth"),
        client.get("email"),
    )
    return any(str(value or "").strip().lower() == needle for value in values)


def _find_pairs(
    inbounds: list[dict[str, Any]], external_ref: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for inbound in inbounds:
        for client in _clients(inbound):
            if _matches(client, external_ref):
                out.append((inbound, client))
    return out


def _stats(inbound: dict[str, Any], email: str) -> tuple[int, int]:
    needle = str(email or "").strip().lower()
    rows = inbound.get("clientStats")
    if not isinstance(rows, list):
        return (0, 0)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("email") or "").strip().lower() == needle:
            return (_safe_int(row.get("up")), _safe_int(row.get("down")))
    return (0, 0)


def _parse_inbound_ids(raw: str) -> list[int] | None:
    text = str(raw or "").strip().replace("،", ",")
    if not text:
        return None
    if text == "0":
        return []
    out: list[int] = []
    for part in text.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise PanelError("invalid X-UI inbound ids") from exc
        if value <= 0:
            raise PanelError("invalid X-UI inbound ids")
        if value not in out:
            out.append(value)
    return out or None


def _select_inbounds(
    target: PanelTarget, inbounds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in inbounds
        if bool(row.get("enable", True))
        and str(row.get("protocol") or "").strip().lower() in _SUPPORTED_PROTOCOLS
    ]
    candidates.sort(key=lambda row: _safe_int(row.get("id")))
    wanted = _parse_inbound_ids(target.xui_inbound_ids)
    if wanted is None:
        if not candidates:
            raise PanelError("no active X-UI inbound is available")
        return [candidates[0]]
    if wanted == []:
        if not candidates:
            raise PanelError("no active X-UI inbound is available")
        return candidates
    by_id = {_safe_int(row.get("id")): row for row in candidates}
    selected: list[dict[str, Any]] = []
    for inbound_id in wanted:
        row = by_id.get(inbound_id)
        if row is None:
            raise PanelError("configured X-UI inbound is unavailable")
        selected.append(row)
    return selected


def _safe_email(request: ProvisionRequest) -> str:
    return f"wl-t{int(request.tenant_id)}-s{int(request.subscription_id)}"[:64]


def _new_client(
    protocol: str,
    *,
    external_ref: str,
    email: str,
    traffic_bytes: int,
    expires_at: str,
    sanaei: bool,
) -> dict[str, Any]:
    proto = str(protocol or "").strip().lower()
    client: dict[str, Any] = {
        "email": email,
        "subId": external_ref,
        "totalGB": max(0, int(traffic_bytes)),
        "expiryTime": _expiry_ms(expires_at),
        "enable": True,
        "limitIp": 0,
        "reset": 0,
        "comment": "",
    }
    if proto in {"trojan", "hysteria", "hysteria2", "shadowsocks"}:
        client["password"] = external_ref
        if proto in {"hysteria", "hysteria2"}:
            client["auth"] = external_ref
    else:
        client["id"] = external_ref
    client["tgId"] = 0 if sanaei else email
    return client


def _sanaei_update_payload(
    client: dict[str, Any],
    *,
    total_bytes: int | None = None,
    expires_at: str | None = None,
    enabled: bool | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    email = str(client.get("email") or "").strip()
    sub_id = str(client.get("subId") or client.get("uuid") or "").strip()
    uuid_field = str(client.get("uuid") or "").strip()
    id_field = str(client.get("id") or "").strip()
    xray_id = (
        uuid_field
        if "-" in uuid_field
        else sub_id
        if "-" in sub_id
        else id_field
        if "-" in id_field
        else sub_id or uuid_field
    )
    result: dict[str, Any] = {
        "email": email,
        "subId": sub_id or xray_id,
        "totalGB": (
            max(0, int(total_bytes))
            if total_bytes is not None
            else max(0, _safe_int(client.get("totalGB")))
        ),
        "expiryTime": (
            _expiry_ms(expires_at)
            if expires_at is not None
            else _safe_int(client.get("expiryTime"))
        ),
        "enable": (
            bool(enabled)
            if enabled is not None
            else bool(client.get("enable", True))
        ),
        "tgId": _safe_int(client.get("tgId")),
        "limitIp": max(0, _safe_int(client.get("limitIp"))),
        "comment": (
            str(comment)
            if comment is not None
            else str(client.get("comment") or "")
        ),
    }
    if xray_id:
        result["uuid"] = xray_id
        result["id"] = xray_id
    if client.get("password"):
        result["password"] = str(client.get("password"))
    if client.get("auth"):
        result["auth"] = str(client.get("auth"))
    if client.get("flow"):
        result["flow"] = str(client.get("flow"))
    if "limitHwid" in client:
        result["limitHwid"] = max(0, _safe_int(client.get("limitHwid")))
    return result


def _credential(target: PanelTarget, secret: str) -> dict[str, str]:
    flavor = str(target.xui_flavor or "").strip().lower()
    if flavor not in {"sanaei", "alireza"}:
        raise PanelError("X-UI flavor is not configured")
    raw = str(secret or "").strip()
    if not raw:
        raise PanelError("X-UI credential is unavailable")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        stored_flavor = str(parsed.get("flavor") or flavor).strip().lower()
        if stored_flavor != flavor:
            raise PanelError("X-UI credential flavor mismatch")
        username = str(parsed.get("username") or "").strip()
        password = str(parsed.get("password") or "").strip()
        header = str(parsed.get("secret_header") or "").strip()
        token = str(parsed.get("api_token") or "").strip()
        if not (flavor == "sanaei" and token) and (not username or not password):
            raise PanelError("X-UI credentials are unavailable")
        return {
            "flavor": flavor,
            "api_token": token,
            "username": username,
            "password": password,
            "secret_header": header,
        }

    if flavor == "sanaei":
        return {"flavor": flavor, "api_token": raw}
    parts = raw.split("|", 2)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise PanelError("Alireza credentials are unavailable")
    return {
        "flavor": flavor,
        "username": parts[0].strip(),
        "password": parts[1].strip(),
        "secret_header": parts[2].strip() if len(parts) > 2 else "",
    }


class _Session:
    def __init__(
        self,
        *,
        client: httpx.Client,
        target: PanelTarget,
        credential: dict[str, str],
    ) -> None:
        self.client = client
        self.target = target
        self.credential = credential
        self.base = _clean_base(target.endpoint)
        self.flavor = credential["flavor"]
        self.token_auth = self.flavor == "sanaei" and bool(credential.get("api_token"))

    @property
    def modern_clients(self) -> bool:
        return self.flavor == "sanaei" and self.token_auth

    @property
    def api_base(self) -> str:
        return (
            f"{self.base}/panel/api"
            if self.flavor == "sanaei"
            else f"{self.base}/xui/API"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json, text/plain, */*"}
        if self.token_auth:
            headers["Authorization"] = f"Bearer {self.credential['api_token']}"
        elif self.credential.get("secret_header"):
            headers[_SECRET_HEADER] = self.credential["secret_header"]
        try:
            response = self.client.request(
                method,
                f"{self.api_base}/{path.lstrip('/')}",
                headers=headers,
                json=payload,
            )
        except httpx.TransportError as exc:
            raise PanelError("X-UI connection failed") from exc
        if response.status_code >= 400:
            raise _StatusError(response.status_code)
        if not response.content:
            return None
        try:
            data = response.json()
        except ValueError:
            return response.text
        if isinstance(data, dict):
            if data.get("success") is False:
                raise PanelError("X-UI API rejected the request")
            if "obj" in data:
                return data.get("obj")
        return data


class XuiPanelAdapter:
    """Synchronous PanelAdapter implementation for Sanaei and Alireza."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = float(timeout_seconds if timeout_seconds is not None else _timeout())

    def _client(self, verify: bool | ssl.SSLContext) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout,
            verify=verify,
            transport=self._transport,
            follow_redirects=False,
        )

    def _authenticate(
        self,
        client: httpx.Client,
        target: PanelTarget,
        credential: dict[str, str],
    ) -> _Session:
        session = _Session(client=client, target=target, credential=credential)
        if session.token_auth:
            try:
                data = session.request("GET", "inbounds/list")
                if not isinstance(data, list):
                    raise PanelError("X-UI returned an invalid inbound list")
                return session
            except _StatusError as exc:
                if exc.status_code not in {401, 403} or not (
                    credential.get("username") and credential.get("password")
                ):
                    raise
                session.token_auth = False
        try:
            response = client.post(
                f"{_clean_base(target.endpoint)}/login",
                json={
                    "username": credential["username"],
                    "password": credential["password"],
                },
            )
        except httpx.TransportError as exc:
            raise PanelError("X-UI connection failed") from exc
        if response.status_code >= 400:
            raise PanelError("X-UI authentication failed")
        if response.content:
            try:
                data = response.json()
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("success") is False:
                raise PanelError("X-UI authentication failed")
        return session

    @contextmanager
    def _session(
        self, target: PanelTarget, secret: str
    ) -> Iterator[_Session]:
        if str(target.kind or "").strip().lower() != "xui":
            raise PanelError("X-UI adapter received the wrong panel kind")
        credential = _credential(target, secret)
        mode = _ssl_mode()
        attempts: list[bool | ssl.SSLContext]
        if mode == "insecure":
            attempts = [_insecure_context()]
        elif mode == "auto":
            attempts = [True, _insecure_context()]
        else:
            attempts = [True]

        selected_client: httpx.Client | None = None
        selected_session: _Session | None = None
        last_error: BaseException | None = None
        for index, verify in enumerate(attempts):
            client = self._client(verify)
            try:
                selected_session = self._authenticate(
                    client, target, credential
                )
                selected_client = client
                break
            except PanelError as exc:
                client.close()
                last_error = exc
                cause = exc.__cause__
                if (
                    mode == "auto"
                    and index == 0
                    and cause is not None
                    and _looks_like_tls_error(cause)
                ):
                    continue
                raise
        if selected_client is None or selected_session is None:
            raise PanelError("X-UI connection failed") from last_error
        try:
            yield selected_session
        finally:
            selected_client.close()

    def _list_inbounds(self, session: _Session) -> list[dict[str, Any]]:
        path = "inbounds/list" if session.flavor == "sanaei" else "inbounds/"
        data = session.request("GET", path)
        if not isinstance(data, list):
            raise PanelError("X-UI returned an invalid inbound list")
        return [dict(item) for item in data if isinstance(item, dict)]

    def _list_sanaei_clients(self, session: _Session) -> list[dict[str, Any]]:
        try:
            data = session.request("GET", "clients/list")
        except _StatusError as exc:
            if exc.status_code == 404:
                return []
            raise
        if not isinstance(data, list):
            raise PanelError("Sanaei returned an invalid client list")
        return [dict(item) for item in data if isinstance(item, dict)]

    @staticmethod
    def _find_sanaei_client(
        clients: list[dict[str, Any]], external_ref: str
    ) -> dict[str, Any] | None:
        for client in clients:
            if _matches(client, external_ref):
                return client
        return None

    def _online_set(self, session: _Session) -> set[str]:
        paths = (
            ("clients/onlines", "inbounds/onlines")
            if session.flavor == "sanaei"
            else ("inbounds/onlines", "clients/onlines")
        )
        for path in paths:
            try:
                data = session.request("POST", path)
            except PanelError:
                continue
            result: set[str] = set()
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        value = item.strip()
                        if value:
                            result.add(value.lower())
                    elif isinstance(item, dict):
                        for key in ("email", "id", "subId", "uuid"):
                            value = str(item.get(key) or "").strip()
                            if value:
                                result.add(value.lower())
            if result:
                return result
        return set()

    def _last_online_map(self, session: _Session) -> dict[str, str]:
        paths = (
            ("clients/lastOnline", "inbounds/lastOnline")
            if session.flavor == "sanaei"
            else ("inbounds/lastOnline", "clients/lastOnline")
        )
        for path in paths:
            try:
                data = session.request("POST", path)
            except PanelError:
                continue
            if not isinstance(data, dict):
                continue
            result: dict[str, str] = {}
            for identity, timestamp in data.items():
                rendered = _timestamp_string(timestamp)
                if rendered:
                    result[str(identity).strip().lower()] = rendered
            return result
        return {}

    def _snapshot(
        self,
        session: _Session,
        external_ref: str,
        *,
        client: dict[str, Any] | None = None,
        inbounds: list[dict[str, Any]] | None = None,
        online: set | None = None,
        last_map: dict | None = None,
    ) -> PanelUserResult:
        inbound_rows = inbounds if inbounds is not None else self._list_inbounds(session)
        pairs = _find_pairs(inbound_rows, external_ref)
        chosen = dict(client) if isinstance(client, dict) else None
        if chosen is None and pairs:
            chosen = dict(pairs[0][1])
        if chosen is None and session.modern_clients:
            clients = self._list_sanaei_clients(session)
            chosen = self._find_sanaei_client(clients, external_ref)
        if chosen is None:
            raise PanelError("X-UI user was not found")

        usage_bytes = 0
        if pairs:
            for inbound, pair_client in pairs:
                up, down = _stats(inbound, str(pair_client.get("email") or ""))
                usage_bytes += up + down
        if usage_bytes == 0:
            traffic = chosen.get("traffic")
            if isinstance(traffic, dict):
                usage_bytes = _safe_int(traffic.get("up")) + _safe_int(
                    traffic.get("down")
                )
            if usage_bytes == 0:
                usage_bytes = _safe_int(chosen.get("up")) + _safe_int(
                    chosen.get("down")
                )

        online = self._online_set(session) if online is None else online
        last_map = self._last_online_map(session) if last_map is None else last_map
        identities = {
            str(chosen.get(key) or "").strip().lower()
            for key in ("email", "subId", "uuid", "id", "password", "auth")
            if str(chosen.get(key) or "").strip()
        }
        if pairs:
            for _, pair_client in pairs:
                identities.add(str(pair_client.get("email") or "").strip().lower())
        is_online = any(identity in online for identity in identities if identity)
        last_online = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if is_online
            else None
        )
        if not last_online:
            for identity in identities:
                if identity and identity in last_map:
                    last_online = last_map[identity]
                    break
        if not last_online:
            for key in ("lastOnline", "last_online", "onlineAt"):
                last_online = _timestamp_string(chosen.get(key))
                if last_online:
                    break

        ref = str(
            chosen.get("subId")
            or chosen.get("uuid")
            or external_ref
            or chosen.get("id")
            or ""
        ).strip()
        if not ref:
            raise PanelError("X-UI user response has no identifier")
        return PanelUserResult(
            external_ref=ref,
            usage_bytes=max(0, int(usage_bytes)),
            active=bool(chosen.get("enable", True)),
            traffic_bytes=max(0, _safe_int(chosen.get("totalGB"))),
            expires_at=_expiry_iso(chosen.get("expiryTime")),
            last_online=last_online,
            subscription_url=self.subscription_link(
                target=session.target, external_ref=ref
            ),
            name=str(chosen.get("name") or chosen.get("email") or ref),
            comment=str(chosen.get("comment") or ""),
        )

    def _existing(
        self, session: _Session, external_ref: str
    ) -> PanelUserResult | None:
        try:
            if session.modern_clients:
                clients = self._list_sanaei_clients(session)
                found = self._find_sanaei_client(clients, external_ref)
                if found is not None:
                    return self._snapshot(
                        session, external_ref, client=found
                    )
            inbounds = self._list_inbounds(session)
            if _find_pairs(inbounds, external_ref):
                return self._snapshot(
                    session, external_ref, inbounds=inbounds
                )
        except PanelError:
            raise
        return None

    def list_users(self, *, target: PanelTarget, secret: str) -> list[dict]:
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            clients = (self._list_sanaei_clients(session) if session.modern_clients
                       else [c for i in inbounds for c in _clients(i)])
            online, last = self._online_set(session), self._last_online_map(session)
            refs, result = set(), []
            for client in clients:
                ref = str(client.get("subId") or client.get("uuid") or client.get("id") or client.get("password") or "")
                if not ref or ref in refs:
                    continue
                refs.add(ref)
                row = asdict(self._snapshot(session, ref, client=client,
                    inbounds=inbounds, online=online, last_map=last))
                try:
                    note = decode_panel_note(row["comment"])
                    if isinstance(note, dict):
                        row["name"] = str(note.get("name") or row["name"])
                        row["comment"] = str(note.get("note") or "")
                except (ValueError, TypeError):
                    pass
                result.append(row)
            return result

    def update_user(self, *, target: PanelTarget, secret: str,
                    external_ref: str, changes: dict) -> PanelUserResult:
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            pairs = _find_pairs(inbounds, external_ref)
            if session.modern_clients:
                client = self._find_sanaei_client(self._list_sanaei_clients(session), external_ref)
                pairs = [(None, client)] if client else []
            if not pairs:
                raise PanelError("X-UI user was not found")
            for inbound, client in pairs:
                updated = _sanaei_update_payload(client) if session.modern_clients else dict(client)
                if "traffic_bytes" in changes:
                    updated["totalGB"] = int(changes["traffic_bytes"])
                if "expires_at" in changes:
                    updated["expiryTime"] = _expiry_ms(changes["expires_at"])
                if "comment" in changes or "name" in changes:
                    current = str(client.get("comment") or "")
                    try:
                        note = decode_panel_note(current)
                        if not isinstance(note, dict):
                            note = {"note": current}
                    except ValueError:
                        note = {"note": current}
                    if "name" in changes:
                        note["name"] = changes["name"]
                    if "comment" in changes:
                        note["note"] = changes["comment"]
                    updated["comment"] = json.dumps(note, ensure_ascii=False)
                email = str(client.get("email") or "")
                if session.modern_clients:
                    session.request("POST", f"clients/update/{quote(email, safe='')}", payload=updated)
                    if changes.get("reset_usage"):
                        session.request("POST", f"clients/resetTraffic/{quote(email, safe='')}")
                else:
                    route_id = _client_identity(client, str(inbound.get("protocol") or "").lower())
                    session.request("POST", f"inbounds/updateClient/{quote(route_id, safe='')}",
                        payload={"id": _safe_int(inbound.get("id")), "settings": json.dumps({"clients": [updated]})})
                    if changes.get("reset_usage"):
                        identity = email if session.flavor == "sanaei" else route_id
                        session.request("POST", f"inbounds/{_safe_int(inbound.get('id'))}/resetClientTraffic/{quote(identity, safe='')}")
            result = self._existing(session, external_ref)
            if result is None:
                raise PanelError("X-UI edit could not be verified")
            return result

    def inspect_connection(self, *, target: PanelTarget, secret: str) -> dict:
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            selected = _select_inbounds(target, inbounds) if inbounds else []
            if not inbounds and target.xui_inbound_ids not in {"", "0"}:
                raise PanelError("configured X-UI inbound is unavailable")
            self.subscription_link(target=target, external_ref="connection-test")
            return {"connected": True, "inbounds": [
                {"id": str(x.get("id") or ""), "protocol": str(x.get("protocol") or ""),
                 "remark": str(x.get("remark") or ""),
                 "enabled": bool(x.get("enable", True))} for x in inbounds],
                "selected_inbound_ids": [str(x["id"]) for x in selected],
                "users_count": len({str(c.get("email") or c.get("id") or "")
                    for x in inbounds for c in _clients(x)})}

    def provision(
        self,
        *,
        target: PanelTarget,
        secret: str,
        request: ProvisionRequest,
    ) -> ProvisionResult:
        external_ref = request.external_ref or str(
            uuid.uuid5(
                _UUID_NAMESPACE,
                f"whitelabel:{request.idempotency_key}",
            )
        )
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            selected = _select_inbounds(target, inbounds)
            base_email = _safe_email(request)

            if session.modern_clients:
                existing = self._existing(session, external_ref)
                if existing is not None:
                    return ProvisionResult(
                        external_ref=existing.external_ref,
                        subscription_url=existing.subscription_url,
                    )
                first_protocol = str(
                    selected[0].get("protocol") or ""
                ).strip().lower()
                client = _new_client(
                    first_protocol,
                    external_ref=external_ref,
                    email=base_email,
                    traffic_bytes=request.traffic_bytes,
                    expires_at=request.expires_at,
                    sanaei=True,
                )
                client["comment"] = (
                    f"WhiteLabel tenant={int(request.tenant_id)} "
                    f"subscription={int(request.subscription_id)}"
                )
                try:
                    session.request(
                        "POST",
                        "clients/add",
                        payload={
                            "client": client,
                            "inboundIds": [
                                _safe_int(row.get("id")) for row in selected
                            ],
                        },
                    )
                except _StatusError as exc:
                    if exc.status_code not in {400, 409, 422}:
                        raise
                    existing = self._existing(session, external_ref)
                    if existing is None:
                        raise
                snapshot = self._existing(session, external_ref)
                if snapshot is None:
                    snapshot = self._snapshot(
                        session,
                        external_ref,
                        client=client,
                        inbounds=inbounds,
                    )
                return ProvisionResult(
                    external_ref=snapshot.external_ref,
                    subscription_url=snapshot.subscription_url,
                )

            selected_ids = {
                _safe_int(row.get("id")) for row in selected
            }
            existing_pairs = _find_pairs(inbounds, external_ref)
            existing_ids = {
                _safe_int(row.get("id")) for row, _ in existing_pairs
            }
            if selected_ids and selected_ids.issubset(existing_ids):
                snapshot = self._snapshot(
                    session,
                    external_ref,
                    inbounds=inbounds,
                )
                return ProvisionResult(
                    external_ref=snapshot.external_ref,
                    subscription_url=snapshot.subscription_url,
                )

            missing = [
                row
                for row in selected
                if _safe_int(row.get("id")) not in existing_ids
            ]
            created: list[tuple[int, str]] = []
            try:
                for index, inbound in enumerate(selected):
                    if inbound not in missing:
                        continue
                    protocol = str(
                        inbound.get("protocol") or ""
                    ).strip().lower()
                    inbound_id = _safe_int(inbound.get("id"))
                    original_index = selected.index(inbound)
                    email = (
                        base_email
                        if original_index == 0
                        else f"{base_email}-{inbound_id}"[:64]
                    )
                    client = _new_client(
                        protocol,
                        external_ref=external_ref,
                        email=email,
                        traffic_bytes=request.traffic_bytes,
                        expires_at=request.expires_at,
                        sanaei=session.flavor == "sanaei",
                    )
                    client["comment"] = (
                        f"WhiteLabel tenant={int(request.tenant_id)} "
                        f"subscription={int(request.subscription_id)}"
                    )
                    client_id = _client_identity(client, protocol)
                    session.request(
                        "POST",
                        "inbounds/addClient",
                        payload={
                            "id": inbound_id,
                            "settings": json.dumps({"clients": [client]}),
                        },
                    )
                    created.append((inbound_id, client_id))
            except PanelError:
                for inbound_id, client_id in created:
                    try:
                        session.request(
                            "POST",
                            f"inbounds/{inbound_id}/delClient/{quote(client_id, safe='')}",
                        )
                    except PanelError:
                        pass
                raise

            refreshed = self._list_inbounds(session)
            refreshed_ids = {
                _safe_int(row.get("id"))
                for row, _ in _find_pairs(refreshed, external_ref)
            }
            if not selected_ids.issubset(refreshed_ids):
                raise PanelError("X-UI create could not be verified on all inbounds")
            snapshot = self._snapshot(
                session,
                external_ref,
                inbounds=refreshed,
            )
            return ProvisionResult(
                external_ref=snapshot.external_ref,
                subscription_url=snapshot.subscription_url,
            )
    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        with self._session(target, secret) as session:
            snapshot = self._existing(session, external_ref)
            if snapshot is None:
                raise PanelError("X-UI user was not found")
            return snapshot

    @staticmethod
    def _renew_marker(idempotency_key: str) -> str:
        text = str(idempotency_key or "").strip()
        if not text:
            return ""
        return "wl-renew:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]

    def renew(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        request: RenewRequest,
    ) -> PanelUserResult:
        marker = self._renew_marker(request.idempotency_key)
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)

            if session.modern_clients:
                clients = self._list_sanaei_clients(session)
                client = self._find_sanaei_client(clients, external_ref)
                if client is None:
                    pairs = _find_pairs(inbounds, external_ref)
                    client = dict(pairs[0][1]) if pairs else None
                if client is None:
                    raise PanelError("X-UI user was not found")
                email = str(client.get("email") or "").strip()
                if not email:
                    raise PanelError("Sanaei user email is unavailable")
                already = bool(marker) and marker in str(client.get("comment") or "")
                comment = str(client.get("comment") or "")
                if marker and not already:
                    comment = f"{comment.strip()} {marker}".strip()[:500]
                updated = _sanaei_update_payload(
                    client,
                    total_bytes=max(0, int(request.traffic_bytes)),
                    expires_at=request.expires_at,
                    enabled=True,
                    comment=comment,
                )
                session.request(
                    "POST",
                    f"clients/update/{quote(email, safe='')}",
                    payload=updated,
                )
                if request.reset_usage and not already:
                    try:
                        session.request(
                            "POST",
                            f"clients/resetTraffic/{quote(email, safe='')}",
                        )
                    except PanelError:
                        pass
                snapshot = self._existing(session, external_ref)
                if snapshot is None:
                    raise PanelError("Sanaei renewal could not be verified")
                return snapshot

            pairs = _find_pairs(inbounds, external_ref)
            if not pairs:
                raise PanelError("X-UI user was not found")
            for inbound, client in pairs:
                protocol = str(inbound.get("protocol") or "").strip().lower()
                route_id = _client_identity(client, protocol)
                if not route_id:
                    raise PanelError("Alireza client identity is unavailable")
                already = bool(marker) and marker in str(client.get("comment") or "")
                updated = dict(client)
                updated["totalGB"] = max(0, int(request.traffic_bytes))
                updated["expiryTime"] = _expiry_ms(request.expires_at)
                updated["enable"] = True
                if marker and not already:
                    updated["comment"] = (
                        f"{str(client.get('comment') or '').strip()} {marker}"
                    ).strip()[:500]
                session.request(
                    "POST",
                    f"inbounds/updateClient/{quote(route_id, safe='')}",
                    payload={
                        "id": _safe_int(inbound.get("id")),
                        "settings": json.dumps({"clients": [updated]}),
                    },
                )
                if request.reset_usage and not already:
                    try:
                        session.request(
                            "POST",
                            f"inbounds/{_safe_int(inbound.get('id'))}/resetClientTraffic/{quote(str(client.get('email') or '') if session.flavor == 'sanaei' else route_id, safe='')}",
                        )
                    except PanelError:
                        pass
            snapshot = self._snapshot(
                session,
                external_ref,
                inbounds=self._list_inbounds(session),
            )
            return snapshot

    def set_enabled(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        enabled: bool,
    ) -> PanelUserResult:
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            if session.modern_clients:
                clients = self._list_sanaei_clients(session)
                client = self._find_sanaei_client(clients, external_ref)
                if client is None:
                    raise PanelError("X-UI user was not found")
                email = str(client.get("email") or "").strip()
                updated = _sanaei_update_payload(
                    client,
                    enabled=bool(enabled),
                )
                session.request(
                    "POST",
                    f"clients/update/{quote(email, safe='')}",
                    payload=updated,
                )
            else:
                pairs = _find_pairs(inbounds, external_ref)
                if not pairs:
                    raise PanelError("X-UI user was not found")
                for inbound, client in pairs:
                    protocol = str(inbound.get("protocol") or "").strip().lower()
                    route_id = _client_identity(client, protocol)
                    updated = dict(client)
                    updated["enable"] = bool(enabled)
                    session.request(
                        "POST",
                        f"inbounds/updateClient/{quote(route_id, safe='')}",
                        payload={
                            "id": _safe_int(inbound.get("id")),
                            "settings": json.dumps({"clients": [updated]}),
                        },
                    )

            snapshot = self._existing(session, external_ref)
            if snapshot is None or snapshot.active != bool(enabled):
                raise PanelError("X-UI account state change could not be verified")
            return snapshot

    def validate_identity_rotation(self, *, target: PanelTarget, secret: str,
                                   external_ref: str) -> None:
        with self._session(target, secret) as session:
            pairs = _find_pairs(self._list_inbounds(session), external_ref)
            if any(str(i.get("protocol") or "").lower() == "wireguard" for i, _ in pairs):
                raise PanelError("WireGuard credential rotation is unsupported")
            if self._existing(session, external_ref) is None:
                raise PanelError("X-UI user was not found")

    def rotate_identity(self, *, target: PanelTarget, secret: str,
                        external_ref: str, new_ref: str) -> PanelUserResult:
        with self._session(target, secret) as session:
            inbounds = self._list_inbounds(session)
            pairs = _find_pairs(inbounds, external_ref)
            # A interrupted multi-inbound change resumes remaining old clients.
            if any(str(i.get("protocol") or "").lower() == "wireguard"
                   for i, _ in pairs):
                raise PanelError("WireGuard credential rotation is unsupported")
            if session.modern_clients:
                client = self._find_sanaei_client(
                    self._list_sanaei_clients(session), external_ref)
                if client is not None:
                    updated = _sanaei_update_payload(client)
                    updated.update(uuid=new_ref, id=new_ref, subId=new_ref)
                    for key in ("password", "auth"):
                        if client.get(key):
                            updated[key] = new_ref
                    session.request("POST",
                        f"clients/update/{quote(str(client.get('email') or ''), safe='')}",
                        payload=updated)
            else:
                for inbound, client in pairs:
                    protocol = str(inbound.get("protocol") or "").lower()
                    route_id = _client_identity(client, protocol)
                    updated = dict(client)
                    updated["subId"] = new_ref
                    for key in ("id", "uuid", "password", "auth"):
                        if client.get(key):
                            updated[key] = new_ref
                    session.request("POST",
                        f"inbounds/updateClient/{quote(route_id, safe='')}",
                        payload={"id": _safe_int(inbound.get("id")),
                                 "settings": json.dumps({"clients": [updated]})})
            refreshed = self._list_inbounds(session)
            if _find_pairs(refreshed, external_ref) or (session.modern_clients and
                self._find_sanaei_client(self._list_sanaei_clients(session), external_ref) is not None):
                raise PanelError("X-UI old credentials are still present")
            result = self._existing(session, new_ref)
            if result is None or result.external_ref != new_ref:
                raise PanelError("X-UI credential rotation could not be verified")
            return result

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        with self._session(target, secret) as session:
            if session.modern_clients:
                clients = self._list_sanaei_clients(session)
                client = self._find_sanaei_client(clients, external_ref)
                if client is None:
                    # Idempotent delete.
                    return
                email = str(client.get("email") or "").strip()
                session.request(
                    "POST", f"clients/del/{quote(email, safe='')}"
                )
                return

            inbounds = self._list_inbounds(session)
            pairs = _find_pairs(inbounds, external_ref)
            if not pairs:
                return
            errors = 0
            for inbound, client in pairs:
                protocol = str(inbound.get("protocol") or "").strip().lower()
                route_id = _client_identity(client, protocol)
                try:
                    session.request(
                        "POST",
                        f"inbounds/{_safe_int(inbound.get('id'))}/delClient/{quote(route_id, safe='')}",
                    )
                except PanelError:
                    errors += 1
            if errors:
                remaining = _find_pairs(self._list_inbounds(session), external_ref)
                if remaining:
                    raise PanelError("X-UI delete could not be verified")

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        user = self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )
        return UsageResult(
            usage_bytes=user.usage_bytes,
            active=user.active,
            last_online=user.last_online,
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        ref = str(external_ref or "").strip()
        if not ref:
            raise PanelError("X-UI subscription identifier is unavailable")
        origin = _clean_origin(target.xui_public_origin, target.endpoint)
        return f"{origin}{_sub_path(target)}{quote(ref, safe='-._~')}"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        del secret
        url = self.subscription_link(target=target, external_ref=external_ref)
        mode = _ssl_mode()
        attempts: list[bool | ssl.SSLContext]
        if mode == "insecure":
            attempts = [_insecure_context()]
        elif mode == "auto":
            attempts = [True, _insecure_context()]
        else:
            attempts = [True]
        last_error: BaseException | None = None
        for index, verify in enumerate(attempts):
            try:
                with httpx.Client(
                    timeout=self._timeout,
                    verify=verify,
                    transport=self._transport,
                    follow_redirects=True,
                ) as client:
                    response = client.get(
                        url, headers={"Accept": "text/plain,*/*"}
                    )
                if response.status_code >= 400:
                    raise _StatusError(response.status_code)
                text = response.text.strip()
                if text:
                    return text
                raise PanelError("X-UI subscription is empty")
            except httpx.TransportError as exc:
                last_error = exc
                if mode == "auto" and index == 0 and _looks_like_tls_error(exc):
                    continue
                raise PanelError("X-UI subscription fetch failed") from exc
        raise PanelError("X-UI subscription fetch failed") from last_error
