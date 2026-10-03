"""Independent live X-NET adapter for TenantRuntime.

The adapter uses X-NET's documented management API and the shared PanelAdapter
contract. Persistent API tokens are preferred; if a build rejects that token,
an encrypted admin username/password can provide a one-request JWT fallback.
Customer subscription links use X-NET's separate public subscription listener.
"""

from __future__ import annotations

from TenantRuntime.panels import decode_panel_note

from dataclasses import asdict

import base64
import hashlib
import json
import os
import ssl
import threading
import time
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

_UUID_NAMESPACE = uuid.UUID("4d16e0f9-0a8d-4e23-a0f7-fbcb18f97d9a")
_JWT_SKEW_SECONDS = 30
_JWT_FALLBACK_TTL = 15 * 60


class _StatusError(PanelError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"X-NET returned HTTP {int(status_code)}")
        self.status_code = int(status_code)


def _timeout() -> float:
    raw = str(os.getenv("XNET_API_TIMEOUT_SECONDS", "18") or "").strip()
    try:
        return max(3.0, min(float(raw), 90.0))
    except ValueError:
        return 18.0


def _ssl_mode() -> str:
    mode = str(os.getenv("XNET_SSL_MODE", "secure") or "").strip().lower()
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
        raise PanelError("invalid X-NET endpoint")
    return text


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value if value is not None else default))
    except (TypeError, ValueError):
        return int(default)


def _parse_dt(value: object) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1_000_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: object) -> str | None:
    parsed = _parse_dt(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _client_uuid(client: dict[str, Any]) -> str:
    return str(
        client.get("uuid")
        or client.get("subscriptionUuid")
        or client.get("subscription_uuid")
        or ""
    ).strip()


def _client_status(client: dict[str, Any]) -> str:
    return str(client.get("status") or "active").strip().lower()


def _find_pairs(
    inbounds: list[dict[str, Any]], external_ref: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    wanted = str(external_ref or "").strip().lower()
    if not wanted:
        return []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for inbound in inbounds:
        clients = inbound.get("clients")
        if not isinstance(clients, list):
            continue
        for client in clients:
            if not isinstance(client, dict):
                continue
            identities = {
                _client_uuid(client).lower(),
                str(client.get("id") or "").strip().lower(),
            }
            if wanted in identities:
                pairs.append((inbound, dict(client)))
    return pairs


def _selected_inbound_ids(
    target: PanelTarget, inbounds: list[dict[str, Any]]
) -> list[str]:
    active = [
        str(row.get("id") or "").strip()
        for row in inbounds
        if str(row.get("id") or "").strip() and bool(row.get("enabled", True))
    ]
    if not active:
        raise PanelError("no X-NET inbound is available")

    raw = str(target.xnet_inbound_ids or "").strip()
    if not raw:
        return [active[0]]
    if raw == "0":
        return active
    requested = [
        part.strip()
        for part in raw.replace("،", ",").split(",")
        if part.strip()
    ]
    allowed = set(active)
    if any(item not in allowed for item in requested):
        raise PanelError("configured X-NET inbound is unavailable")
    return requested or [active[0]]


def _credential(secret: str) -> dict[str, str]:
    raw = str(secret or "").strip()
    if not raw:
        raise PanelError("X-NET credential is unavailable")
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        token = str(parsed.get("api_token") or "").strip()
        username = str(parsed.get("username") or "admin").strip() or "admin"
        password = str(parsed.get("password") or "").strip()
        if not token and not password:
            raise PanelError("X-NET credential is unavailable")
        return {
            "api_token": token,
            "username": username,
            "password": password,
        }
    # Backward-compatible single secret = persistent API token.
    return {"api_token": raw, "username": "admin", "password": ""}


def _jwt_expiry(token: str) -> float:
    now = time.time()
    try:
        parts = str(token or "").split(".")
        if len(parts) >= 2:
            payload = parts[1] + ("=" * (-len(parts[1]) % 4))
            decoded = json.loads(
                base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
            )
            exp = float(decoded.get("exp") or 0)
            if exp > now + _JWT_SKEW_SECONDS:
                return exp - _JWT_SKEW_SECONDS
    except Exception:
        pass
    return now + _JWT_FALLBACK_TTL


class _Session:
    def __init__(
        self,
        *,
        adapter: "XnetPanelAdapter",
        client: httpx.Client,
        target: PanelTarget,
        credential: dict[str, str],
    ) -> None:
        self.adapter = adapter
        self.client = client
        self.target = target
        self.base = _clean_base(target.xnet_api_url or target.endpoint)
        self.credential = credential
        self._api_token_rejected = False
        self._jwt = ""
        # Changing credentials must never pass a probe using the previous JWT.
        digest = hashlib.sha256(json.dumps(credential, sort_keys=True).encode()).hexdigest()
        self._cache_key = (self.base, digest)

    def _login(self, *, force: bool = False) -> str:
        username = self.credential["username"]
        password = self.credential["password"]
        if not password:
            raise PanelError("X-NET admin fallback credentials are unavailable")

        cache_key = self._cache_key
        if not force:
            cached = self.adapter._cached_jwt(cache_key)
            if cached:
                self._jwt = cached
                return cached

        try:
            response = self.client.post(
                f"{self.base}/api/auth/login",
                headers={"Accept": "application/json"},
                json={"username": username, "password": password},
            )
        except httpx.TransportError as exc:
            raise PanelError("X-NET authentication failed") from exc
        if response.status_code >= 400:
            raise PanelError("X-NET authentication failed")
        try:
            data = response.json()
        except ValueError as exc:
            raise PanelError("X-NET authentication returned invalid data") from exc
        if not isinstance(data, dict):
            raise PanelError("X-NET authentication returned invalid data")
        if bool(data.get("requires2fa")):
            raise PanelError("X-NET fallback account requires 2FA")
        token = str(data.get("token") or "").strip()
        if not token:
            raise PanelError("X-NET authentication returned no token")
        self._jwt = token
        self.adapter._store_jwt(cache_key, token, _jwt_expiry(token))
        return token

    def _auth_token(self) -> tuple[str, bool]:
        api_token = str(self.credential.get("api_token") or "").strip()
        if api_token and not self._api_token_rejected:
            return api_token, True
        if self._jwt:
            return self._jwt, False
        return self._login(force=False), False

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        allow_not_found: bool = False,
    ) -> Any:
        attempts = 0
        while True:
            attempts += 1
            token = ""
            using_api_token = False
            if auth:
                token, using_api_token = self._auth_token()
            headers = {"Accept": "application/json"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = self.client.request(
                    method.upper(),
                    f"{self.base}/{path.lstrip('/')}",
                    headers=headers,
                    json=payload,
                    params=params,
                )
            except httpx.TransportError as exc:
                raise PanelError("X-NET connection failed") from exc

            if allow_not_found and response.status_code == 404:
                return None

            if auth and response.status_code in {401, 403} and attempts <= 2:
                if using_api_token:
                    self._api_token_rejected = True
                    self._jwt = ""
                    if not self.credential.get("password"):
                        raise PanelError(
                            "X-NET API token was rejected and no fallback is configured"
                        )
                    self._login(force=False)
                    continue
                if self.credential.get("password"):
                    self.adapter._drop_jwt(self._cache_key)
                    self._jwt = ""
                    self._login(force=True)
                    continue

            if response.status_code >= 400:
                raise _StatusError(response.status_code)
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise PanelError("X-NET returned invalid JSON") from exc


class XnetPanelAdapter:
    """Synchronous PanelAdapter implementation for X-NET."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = float(
            timeout_seconds if timeout_seconds is not None else _timeout()
        )
        self._jwt_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._jwt_lock = threading.Lock()

    def _cached_jwt(self, key: tuple[str, str]) -> str:
        with self._jwt_lock:
            cached = self._jwt_cache.get(key)
            if cached and cached[0] > time.time() + 5:
                return cached[1]
            self._jwt_cache.pop(key, None)
        return ""

    def _store_jwt(
        self, key: tuple[str, str], token: str, expires_at: float
    ) -> None:
        with self._jwt_lock:
            self._jwt_cache[key] = (float(expires_at), str(token))

    def _drop_jwt(self, key: tuple[str, str]) -> None:
        with self._jwt_lock:
            self._jwt_cache.pop(key, None)

    def _client(self, verify: bool | ssl.SSLContext) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout,
            verify=verify,
            transport=self._transport,
            follow_redirects=False,
        )

    @contextmanager
    def _session(
        self, target: PanelTarget, secret: str
    ) -> Iterator[_Session]:
        if str(target.kind or "").strip().lower() != "xnet":
            raise PanelError("X-NET adapter received the wrong panel kind")
        credential = _credential(secret)
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
            session = _Session(
                adapter=self,
                client=client,
                target=target,
                credential=credential,
            )
            try:
                # Lightweight public probe establishes TLS without consuming
                # login attempts. A protected request later performs auth.
                session.request("GET", "/api/v1/ping", auth=False)
                selected_client = client
                selected_session = session
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
            raise PanelError("X-NET connection failed") from last_error
        try:
            yield selected_session
        finally:
            selected_client.close()

    @staticmethod
    def _inbounds(session: _Session) -> list[dict[str, Any]]:
        data = session.request("GET", "/api/inbounds")
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("inbounds", "items", "data"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [dict(item) for item in rows if isinstance(item, dict)]
        raise PanelError("X-NET returned an invalid inbound list")

    @staticmethod
    def _online_map(session: _Session) -> dict[str, dict[str, Any]]:
        try:
            data = session.request("GET", "/api/online-users")
            rows = data.get("singbox") if isinstance(data, dict) else None
            if isinstance(rows, list):
                result: dict[str, dict[str, Any]] = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    cid = str(
                        row.get("clientId")
                        or row.get("client_id")
                        or row.get("id")
                        or ""
                    ).strip()
                    if cid:
                        result[cid] = dict(row)
                return result
        except PanelError:
            pass

        for path, key in (
            ("/api/traffic/singbox/realtime", "clients"),
            ("/api/traffic/singbox/online", "users"),
        ):
            try:
                data = session.request("GET", path)
            except PanelError:
                continue
            rows = data.get(key) if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue
            result = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if path.endswith("realtime") and not bool(row.get("isOnline")):
                    continue
                cid = str(
                    row.get("clientId")
                    or row.get("client_id")
                    or row.get("id")
                    or ""
                ).strip()
                if cid:
                    result[cid] = dict(row)
            return result
        return {}

    @staticmethod
    def _session_last_seen(session: _Session, client_id: str) -> str | None:
        try:
            data = session.request(
                "GET",
                f"/api/traffic/singbox/clients/{quote(client_id, safe='')}/sessions",
            )
        except PanelError:
            return None
        rows = data.get("sessions") if isinstance(data, dict) else None
        latest: datetime | None = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key in (
                    "lastSeen",
                    "last_seen",
                    "endTime",
                    "end_time",
                    "startTime",
                    "start_time",
                ):
                    parsed = _parse_dt(row.get(key))
                    if parsed is not None and (
                        latest is None or parsed > latest
                    ):
                        latest = parsed
        return latest.isoformat().replace("+00:00", "Z") if latest else None

    def _snapshot(
        self,
        session: _Session,
        external_ref: str,
        *,
        inbounds: list[dict[str, Any]] | None = None,
        online_map: dict | None = None,
        include_history: bool = True,
    ) -> PanelUserResult:
        rows = inbounds if inbounds is not None else self._inbounds(session)
        pairs = _find_pairs(rows, external_ref)
        if not pairs:
            raise PanelError("X-NET user was not found")

        seen_client_ids: set[str] = set()
        usage_bytes = 0
        traffic_bytes = 0
        expiry: datetime | None = None
        last_seen: datetime | None = None
        active = True
        online_map = self._online_map(session) if online_map is None else online_map
        is_online = False

        for _inbound, client in pairs:
            client_id = str(client.get("id") or "").strip()
            dedup_key = client_id or f"anon-{len(seen_client_ids)}"
            if dedup_key not in seen_client_ids:
                seen_client_ids.add(dedup_key)
                usage_bytes += max(0, _safe_int(client.get("trafficUsedBytes")))
            traffic_bytes = max(
                traffic_bytes, max(0, _safe_int(client.get("trafficLimitBytes")))
            )
            status = _client_status(client)
            if status in {"disabled", "inactive", "expired", "blocked"}:
                active = False
            parsed_expiry = _parse_dt(client.get("expireDate"))
            if parsed_expiry is not None and (
                expiry is None or parsed_expiry > expiry
            ):
                expiry = parsed_expiry
            for key in (
                "lastConnectionAt",
                "last_connection_at",
                "lastOnline",
                "last_online",
                "lastSeen",
                "last_seen",
            ):
                parsed_seen = _parse_dt(client.get(key))
                if parsed_seen is not None and (
                    last_seen is None or parsed_seen > last_seen
                ):
                    last_seen = parsed_seen
            if client_id and client_id in online_map:
                is_online = True
            if _safe_int(client.get("activeSessions")) > 0:
                is_online = True

        if is_online:
            last_online = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            if last_seen is None and include_history:
                for client_id in sorted(seen_client_ids):
                    if not client_id or client_id.startswith("anon-"):
                        continue
                    history = _parse_dt(
                        self._session_last_seen(session, client_id)
                    )
                    if history is not None and (
                        last_seen is None or history > last_seen
                    ):
                        last_seen = history
            last_online = (
                last_seen.strftime("%Y-%m-%d %H:%M:%S")
                if last_seen is not None
                else None
            )

        ref = _client_uuid(pairs[0][1]) or str(external_ref or "").strip()
        if not ref:
            raise PanelError("X-NET user response has no identifier")
        return PanelUserResult(
            external_ref=ref,
            usage_bytes=max(0, usage_bytes),
            active=active,
            traffic_bytes=max(0, traffic_bytes),
            expires_at=expiry.isoformat().replace("+00:00", "Z")
            if expiry is not None
            else None,
            last_online=last_online,
            online=is_online,
            subscription_url=self.subscription_link(
                target=session.target, external_ref=ref
            ),
            name=str(pairs[0][1].get("username") or ref),
            comment=str(pairs[0][1].get("remark") or ""),
        )

    @staticmethod
    def _update_body(
        client: dict[str, Any],
        *,
        traffic_bytes: int | None = None,
        expires_at: str | None = None,
        status: str | None = None,
        remark: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for key in (
            "username",
            "email",
            "uuid",
            "password",
            "remark",
            "status",
            "trafficLimitBytes",
            "expireDate",
            "maxConnections",
            "deviceLimit",
            "restrictByIp",
            "flow",
            "cipher",
            "wgPublicKey",
            "wgPrivateKey",
            "wgAddress",
            "wgPresharedKey",
            "extraInboundIds",
        ):
            if key in client:
                body[key] = client.get(key)
        if traffic_bytes is not None:
            body["trafficLimitBytes"] = max(0, int(traffic_bytes))
        if expires_at is not None:
            body["expireDate"] = _iso(expires_at)
        if status is not None:
            body["status"] = str(status)
        if remark is not None:
            body["remark"] = str(remark)
        return body

    @staticmethod
    def _renew_marker(idempotency_key: str) -> str:
        raw = str(idempotency_key or "").strip()
        if not raw:
            return ""
        return "wl-renew:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def list_users(self, *, target: PanelTarget, secret: str) -> list[dict]:
        with self._session(target, secret) as session:
            inbounds, online = self._inbounds(session), self._online_map(session)
            refs = {_client_uuid(c) for i in inbounds for c in (i.get("clients") or []) if _client_uuid(c)}
            result = []
            for ref in sorted(refs):
                row = asdict(self._snapshot(session, ref, inbounds=inbounds,
                                           online_map=online, include_history=False))
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
            pairs = _find_pairs(self._inbounds(session), external_ref)
            if not pairs:
                raise PanelError("X-NET user was not found")
            seen = set()
            for inbound, client in pairs:
                client_id = str(client.get("id") or "")
                if not client_id or client_id in seen:
                    continue
                seen.add(client_id)
                body = self._update_body(client,
                    traffic_bytes=changes.get("traffic_bytes"), expires_at=changes.get("expires_at"))
                if "comment" in changes or "name" in changes:
                    current = str(client.get("remark") or "")
                    try:
                        note = decode_panel_note(current)
                        if not isinstance(note, dict):
                            note = {"note": current}
                    except ValueError:
                        note = {"note": current}
                    for key, field in (("name", "name"), ("comment", "note")):
                        if key in changes:
                            note[field] = changes[key]
                    body["remark"] = json.dumps(note, ensure_ascii=False)
                path = f"/api/inbounds/{quote(str(inbound.get('id') or ''), safe='')}/clients/{quote(client_id, safe='')}"
                session.request("PUT", path, payload=body)
                if changes.get("reset_usage"):
                    session.request("POST", path + "/reset-traffic")
            return self._snapshot(session, external_ref, inbounds=self._inbounds(session))

    def server_stats(self, *, target: PanelTarget, secret: str) -> dict:
        """SellBot-compatible X-NET system, traffic and presence statistics."""
        try:
            users = self.list_users(target=target, secret=secret)
        except PanelError:
            users = []

        now = datetime.now(timezone.utc)
        users_online = 0
        users_today = 0
        users_month = 0
        for user in users:
            if not isinstance(user, dict):
                continue
            online = bool(user.get("online"))
            if online:
                users_online += 1
                users_today += 1
                users_month += 1
                continue
            seen = _parse_dt(user.get("last_online"))
            if seen is None:
                continue
            age = max(0.0, (now - seen).total_seconds())
            if age <= 86400:
                users_today += 1
            if age <= 30 * 86400:
                users_month += 1

        def _number(value: Any, default: float = 0.0) -> float:
            try:
                return float(value if value is not None else default)
            except (TypeError, ValueError):
                return float(default)

        metrics: dict[str, Any] = {}
        traffic: dict[str, Any] = {}
        analytics: dict[str, Any] = {}
        realtime_down = 0.0
        realtime_up = 0.0
        try:
            with self._session(target, secret) as session:
                try:
                    raw = session.request("GET", "/api/metrics")
                    metrics = dict(raw) if isinstance(raw, dict) else {}
                except PanelError:
                    metrics = {}
                try:
                    raw = session.request("GET", "/api/traffic/singbox/summary")
                    traffic = dict(raw) if isinstance(raw, dict) else {}
                except PanelError:
                    traffic = {}
                try:
                    start = datetime.fromtimestamp(
                        now.timestamp() - 30 * 86400,
                        tz=timezone.utc,
                    )
                    raw = session.request(
                        "GET",
                        "/api/traffic/singbox/analytics",
                        params={
                            "from": start.isoformat().replace("+00:00", "Z"),
                            "to": now.isoformat().replace("+00:00", "Z"),
                        },
                    )
                    analytics = dict(raw) if isinstance(raw, dict) else {}
                except PanelError:
                    analytics = {}
                try:
                    tick = session.request("GET", "/api/metrics/tick")
                    network = (
                        tick.get("networkTraffic")
                        if isinstance(tick, dict)
                        and isinstance(tick.get("networkTraffic"), dict)
                        else {}
                    )
                    realtime_down = max(0.0, _number(network.get("down"), 0.0))
                    realtime_up = max(0.0, _number(network.get("up"), 0.0))
                except PanelError:
                    try:
                        live = session.request(
                            "GET", "/api/traffic/singbox/realtime"
                        )
                        rows = (
                            live.get("clients")
                            if isinstance(live, dict)
                            and isinstance(live.get("clients"), list)
                            else []
                        )
                        download_rate = 0.0
                        upload_rate = 0.0
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            download_rate += max(
                                0.0, _number(row.get("downloadRate"), 0.0)
                            )
                            upload_rate += max(
                                0.0, _number(row.get("uploadRate"), 0.0)
                            )
                        realtime_down = download_rate / float(1024 ** 2)
                        realtime_up = upload_rate / float(1024 ** 2)
                    except PanelError:
                        pass
        except PanelError:
            pass

        ram = (
            metrics.get("ramUsage")
            if isinstance(metrics.get("ramUsage"), dict)
            else {}
        )
        storage = (
            metrics.get("storageUsage")
            if isinstance(metrics.get("storageUsage"), dict)
            else {}
        )
        total_upload = max(0, _safe_int(traffic.get("totalUpload"), 0))
        total_download = max(0, _safe_int(traffic.get("totalDownload"), 0))
        today_upload = max(0, _safe_int(traffic.get("todayUpload"), 0))
        today_download = max(0, _safe_int(traffic.get("todayDownload"), 0))

        period_bytes = 0
        active_period_ids: set[str] = set()
        consumers = analytics.get("consumers")
        if isinstance(consumers, list):
            for row in consumers:
                if not isinstance(row, dict):
                    continue
                if str(row.get("kind") or "vpn").strip().lower() == "ssh":
                    continue
                if "periodTotal" in row:
                    used = max(0, _safe_int(row.get("periodTotal"), 0))
                else:
                    used = max(
                        0,
                        _safe_int(row.get("periodUpload"), 0)
                        + _safe_int(row.get("periodDownload"), 0),
                    )
                period_bytes += used
                if used > 0:
                    identity = str(
                        row.get("clientId")
                        or row.get("client_id")
                        or row.get("uuid")
                        or row.get("username")
                        or ""
                    ).strip()
                    if identity:
                        active_period_ids.add(identity)
        elif "periodTotal" in analytics:
            period_bytes = max(0, _safe_int(analytics.get("periodTotal"), 0))
        elif "periodUpload" in analytics or "periodDownload" in analytics:
            period_bytes = max(
                0,
                _safe_int(analytics.get("periodUpload"), 0)
                + _safe_int(analytics.get("periodDownload"), 0),
            )

        if active_period_ids:
            users_month = max(users_month, len(active_period_ids))
        metrics_online = _safe_int(
            metrics.get("onlineUsersCount"),
            _safe_int(traffic.get("activeClients"), users_online),
        )
        users_online = max(users_online, metrics_online)
        users_today = max(users_today, users_online)
        users_month = max(users_month, users_online)

        gib = float(1024 ** 3)
        return {
            "cpu_percent": _number(metrics.get("cpuUsage"), 0.0),
            "cpu_cores": max(1, _safe_int(metrics.get("cpuCores"), 1)),
            "ram_used": _number(ram.get("used"), 0.0),
            "ram_total": max(1.0, _number(ram.get("total"), 1.0)),
            "disk_used": _number(storage.get("used"), 0.0),
            "disk_total": max(1.0, _number(storage.get("total"), 1.0)),
            "users_total": len(users),
            "users_online": users_online,
            "users_today": users_today,
            "users_month": users_month,
            "usage_today_gb": (today_upload + today_download) / gib,
            "usage_30days_gb": (
                period_bytes / gib
                if period_bytes > 0
                else (total_upload + total_download) / gib
            ),
            "traffic_dl": total_download / gib,
            "traffic_ul": total_upload / gib,
            "now_net_recv_mb": realtime_down,
            "now_net_sent_mb": realtime_up,
        }

    def inspect_connection(self, *, target: PanelTarget, secret: str) -> dict:
        with self._session(target, secret) as session:
            inbounds = self._inbounds(session)
            selected = _selected_inbound_ids(target, inbounds) if inbounds else []
            if not inbounds and target.xnet_inbound_ids not in {"", "0"}:
                raise PanelError("configured X-NET inbound is unavailable")
            self.subscription_link(target=target, external_ref="connection-test")
            return {"connected": True, "inbounds": [
                {"id": str(x.get("id") or ""), "protocol": str(x.get("protocol") or ""),
                 "remark": str(x.get("remark") or x.get("name") or ""),
                 "enabled": bool(x.get("enabled", True))} for x in inbounds],
                "selected_inbound_ids": selected,
                "users_count": sum(len(x.get("clients") or []) for x in inbounds)}

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
            inbounds = self._inbounds(session)
            existing = _find_pairs(inbounds, external_ref)
            if existing:
                snapshot = self._snapshot(
                    session, external_ref, inbounds=inbounds
                )
                return ProvisionResult(
                    external_ref=snapshot.external_ref,
                    subscription_url=snapshot.subscription_url,
                )

            target_ids = _selected_inbound_ids(target, inbounds)
            expire_date = _iso(request.expires_at)
            body: dict[str, Any] = {
                "username": request.name or f"wl-t{int(request.tenant_id)}-s{int(request.subscription_id)}",
                "uuid": external_ref,
                "status": "active",
                "trafficLimitBytes": max(0, int(request.traffic_bytes)),
                "autoDisableOnTrafficExhaust": True,
                "expireDate": expire_date,
                "autoDisableOnExpiration": True,
                "maxConnections": 0,
                "remark": (
                    f"WhiteLabel tenant={int(request.tenant_id)} "
                    f"subscription={int(request.subscription_id)}"
                ),
            }
            if len(target_ids) > 1:
                body["extraInboundIds"] = target_ids[1:]

            try:
                session.request(
                    "POST",
                    f"/api/inbounds/{quote(target_ids[0], safe='')}/clients",
                    payload=body,
                )
            except _StatusError as exc:
                if exc.status_code not in {400, 409, 422}:
                    raise
                refreshed = self._inbounds(session)
                if not _find_pairs(refreshed, external_ref):
                    raise

            refreshed = self._inbounds(session)
            if not _find_pairs(refreshed, external_ref):
                raise PanelError("X-NET create could not be verified")
            snapshot = self._snapshot(
                session, external_ref, inbounds=refreshed
            )
            return ProvisionResult(
                external_ref=snapshot.external_ref,
                subscription_url=snapshot.subscription_url,
            )

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        with self._session(target, secret) as session:
            return self._snapshot(session, external_ref)

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
            inbounds = self._inbounds(session)
            pairs = _find_pairs(inbounds, external_ref)
            if not pairs:
                raise PanelError("X-NET user was not found")
            seen: set[str] = set()
            for inbound, client in pairs:
                inbound_id = str(inbound.get("id") or "").strip()
                client_id = str(client.get("id") or "").strip()
                if not inbound_id or not client_id or client_id in seen:
                    continue
                seen.add(client_id)
                already = bool(marker) and marker in str(
                    client.get("remark") or ""
                )
                remark = str(client.get("remark") or "").strip()
                if marker and not already:
                    remark = f"{remark} {marker}".strip()[:500]
                body = self._update_body(
                    client,
                    traffic_bytes=request.traffic_bytes,
                    expires_at=request.expires_at,
                    status="active",
                    remark=remark,
                )
                session.request(
                    "PUT",
                    f"/api/inbounds/{quote(inbound_id, safe='')}/clients/{quote(client_id, safe='')}",
                    payload=body,
                )
                if request.reset_usage and not already:
                    try:
                        session.request(
                            "POST",
                            f"/api/inbounds/{quote(inbound_id, safe='')}/clients/{quote(client_id, safe='')}/reset-traffic",
                        )
                    except PanelError:
                        pass
            refreshed = self._inbounds(session)
            return self._snapshot(
                session, external_ref, inbounds=refreshed
            )

    def set_enabled(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        enabled: bool,
    ) -> PanelUserResult:
        with self._session(target, secret) as session:
            inbounds = self._inbounds(session)
            pairs = _find_pairs(inbounds, external_ref)
            if not pairs:
                raise PanelError("X-NET user was not found")
            seen: set[str] = set()
            for inbound, client in pairs:
                inbound_id = str(inbound.get("id") or "").strip()
                client_id = str(client.get("id") or "").strip()
                if not inbound_id or not client_id or client_id in seen:
                    continue
                seen.add(client_id)
                body = self._update_body(
                    client,
                    status="active" if enabled else "disabled",
                )
                session.request(
                    "PUT",
                    f"/api/inbounds/{quote(inbound_id, safe='')}/clients/{quote(client_id, safe='')}",
                    payload=body,
                )
            snapshot = self._snapshot(
                session,
                external_ref,
                inbounds=self._inbounds(session),
            )
            if snapshot.active != bool(enabled):
                raise PanelError("X-NET account state change could not be verified")
            return snapshot

    def validate_identity_rotation(self, *, target: PanelTarget, secret: str,
                                   external_ref: str) -> None:
        with self._session(target, secret) as session:
            pairs = _find_pairs(self._inbounds(session), external_ref)
            if not pairs:
                raise PanelError("X-NET user was not found")
            if any(str(i.get("protocol") or "").lower() in {"wireguard", "wg"}
                   or c.get("wgPublicKey") for i, c in pairs):
                raise PanelError("WireGuard credential rotation is unsupported")

    def rotate_identity(self, *, target: PanelTarget, secret: str,
                        external_ref: str, new_ref: str) -> PanelUserResult:
        with self._session(target, secret) as session:
            pairs = _find_pairs(self._inbounds(session), external_ref)
            if any(str(i.get("protocol") or "").lower() in {"wireguard", "wg"}
                   or c.get("wgPublicKey") for i, c in pairs):
                raise PanelError("WireGuard credential rotation is unsupported")
            seen = set()
            for inbound, client in pairs:
                client_id = str(client.get("id") or "")
                if not client_id or client_id in seen:
                    continue
                seen.add(client_id)
                body = self._update_body(client)
                body["uuid"] = new_ref
                if client.get("password"):
                    body["password"] = new_ref
                session.request("PUT",
                    f"/api/inbounds/{quote(str(inbound.get('id') or ''), safe='')}/clients/{quote(client_id, safe='')}",
                    payload=body)
            refreshed = self._inbounds(session)
            if _find_pairs(refreshed, external_ref):
                raise PanelError("X-NET old credentials are still present")
            result = self._snapshot(session, new_ref, inbounds=refreshed)
            if result.external_ref != new_ref:
                raise PanelError("X-NET credential rotation could not be verified")
            return result

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        ref = str(external_ref or "").strip()
        if not ref:
            raise PanelError("X-NET subscription identifier is unavailable")
        with self._session(target, secret) as session:
            session.request(
                "DELETE",
                f"/api/v1/subscribers/{quote(ref, safe='')}",
                allow_not_found=True,
            )
            try:
                refreshed = self._inbounds(session)
            except PanelError:
                return
            if _find_pairs(refreshed, ref):
                raise PanelError("X-NET delete could not be verified")

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
            raise PanelError("X-NET subscription identifier is unavailable")

        custom = str(target.xnet_public_origin or "").strip().rstrip("/")
        raw = custom or _clean_base(target.endpoint)
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PanelError("invalid X-NET public origin")

        configured_port = int(target.xnet_sub_port or 0)
        sub_port = configured_port if configured_port > 0 else 2096
        if (
            configured_port <= 0
            and custom
            and parsed.port
            and parsed.port != 8080
        ):
            sub_port = int(parsed.port)
        default_port = (
            parsed.scheme == "https" and sub_port == 443
        ) or (parsed.scheme == "http" and sub_port == 80)
        origin = (
            f"{parsed.scheme}://{parsed.hostname}"
            if default_port
            else f"{parsed.scheme}://{parsed.hostname}:{sub_port}"
        )
        path = str(target.xnet_sub_path or "sub").strip().strip("/") or "sub"
        if ".." in path:
            raise PanelError("invalid X-NET subscription path")
        return f"{origin}/{path}/{quote(ref, safe='-._~')}"

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
                raise PanelError("X-NET subscription is empty")
            except httpx.TransportError as exc:
                last_error = exc
                if mode == "auto" and index == 0 and _looks_like_tls_error(exc):
                    continue
                raise PanelError("X-NET subscription fetch failed") from exc
        raise PanelError("X-NET subscription fetch failed") from last_error
