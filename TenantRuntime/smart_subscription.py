"""Managed multi-node subscription aggregation and public HTTP endpoint."""

from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from Database.connection import connect
from Shared.crypto import TokenCipher, TokenCipherError
from Shared.timeutils import parse_utc, utcnow
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.panels import PanelAdapter, PanelError, build_default_panel_adapter

_CONFIG_SCHEMES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "ssr://",
    "hysteria://",
    "hysteria2://",
    "hy2://",
    "tuic://",
    "anytls://",
    "wireguard://",
)


class SmartSubscriptionError(RuntimeError):
    """Safe public subscription failure."""


@dataclass(frozen=True)
class SmartSubscriptionResponse:
    body: str
    headers: dict[str, str]


def _looks_like_config(line: str) -> bool:
    raw = str(line or "").strip().lower()
    return any(raw.startswith(prefix) for prefix in _CONFIG_SCHEMES)


def _try_decode_base64(raw: str) -> str:
    text = "".join(str(raw or "").strip().split())
    if not text or len(text) < 8:
        return ""
    padded = text + ("=" * (-len(text) % 4))
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded.encode("ascii")).decode("utf-8")
        except Exception:
            continue
        if any(_looks_like_config(line) for line in decoded.splitlines()):
            return decoded
    return ""


def decode_subscription_lines(raw: str) -> list[str]:
    """Accept plain or base64 native subscriptions and keep config lines only."""
    text = str(raw or "").strip()
    if not text:
        return []
    plain_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(_looks_like_config(line) for line in plain_lines):
        return [line for line in plain_lines if _looks_like_config(line)]
    decoded = _try_decode_base64(text)
    if not decoded:
        return []
    return [
        line.strip()
        for line in decoded.splitlines()
        if _looks_like_config(line.strip())
    ]


def _dedup_key(line: str) -> str:
    raw = str(line or "").strip()
    low = raw.lower()
    try:
        if low.startswith("vmess://"):
            encoded = raw.split("://", 1)[1].split("#", 1)[0].strip()
            encoded += "=" * (-len(encoded) % 4)
            data = json.loads(base64.b64decode(encoded).decode("utf-8"))
            return (
                f"vmess:{data.get('id') or ''}@{data.get('add') or ''}:"
                f"{data.get('port') or ''}:{data.get('net') or data.get('type') or ''}"
            ).lower()
        if "://" not in raw:
            return low
        scheme, rest = raw.split("://", 1)
        if "@" not in rest:
            return low
        identity, destination = rest.split("@", 1)
        hostport = re.split(r"[?#/]", destination, maxsplit=1)[0]
        query = ""
        if "?" in rest:
            query = rest.split("?", 1)[1].split("#", 1)[0]
        params = parse_qs(query, keep_blank_values=True)
        network = (
            (params.get("type") or [""])[0]
            or (params.get("net") or [""])[0]
            or scheme
        )
        return (
            f"{scheme.lower()}:{identity.split('#', 1)[0]}@{hostport}:{network}"
        ).lower()
    except Exception:
        return low


def aggregate_lines(payloads: list[str]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for payload in payloads:
        for line in decode_subscription_lines(payload):
            key = _dedup_key(line)
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return lines


def smart_subscription_url(public_base_url: str, code: str) -> str:
    base = str(public_base_url or "").strip().rstrip("/")
    token = str(code or "").strip()
    if not base or not token:
        return ""
    parsed = urlparse(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return f"{base}/sub/{quote(token, safe='-._~')}/all.b64"


class SmartSubscriptionService:
    def __init__(
        self,
        *,
        db_path: str,
        cipher: TokenCipher,
        panel_adapter_factory: Callable[[], PanelAdapter] = build_default_panel_adapter,
    ) -> None:
        self.db_path = str(db_path)
        self.cipher = cipher
        self.panel_adapter_factory = panel_adapter_factory

    @staticmethod
    def _subscription_id(target: str) -> int:
        raw = str(target or "").strip()
        if not raw.startswith("subscription:"):
            raise SmartSubscriptionError("smart subscription target is invalid")
        try:
            value = int(raw.split(":", 1)[1])
        except (TypeError, ValueError) as exc:
            raise SmartSubscriptionError("smart subscription target is invalid") from exc
        if value <= 0:
            raise SmartSubscriptionError("smart subscription target is invalid")
        return value

    def build(self, code: str, *, base64_output: bool = True) -> SmartSubscriptionResponse:
        token = str(code or "").strip()
        if not token or len(token) > 128:
            raise SmartSubscriptionError("subscription was not found")
        conn = connect(self.db_path)
        adapter = self.panel_adapter_factory()
        try:
            link = conn.execute(
                "SELECT * FROM tenant_smart_links WHERE code=? AND status='active'",
                (token,),
            ).fetchone()
            if link is None:
                raise SmartSubscriptionError("subscription was not found")
            tenant_id = int(link["tenant_id"])
            subscription_id = self._subscription_id(str(link["target"]))
            subscription_row = conn.execute(
                "SELECT s.*, p.name AS plan_name, t.owner_telegram_id "
                "FROM tenant_subscriptions s "
                "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
                "JOIN tenants t ON t.id=s.tenant_id "
                "WHERE s.id=? AND s.tenant_id=?",
                (subscription_id, tenant_id),
            ).fetchone()
            if subscription_row is None:
                raise SmartSubscriptionError("subscription was not found")
            subscription = dict(subscription_row)
            if str(subscription["status"]) != "active":
                raise SmartSubscriptionError("subscription is not active")
            try:
                if parse_utc(str(subscription["expires_at"])) <= utcnow():
                    raise SmartSubscriptionError("subscription is expired")
            except (TypeError, ValueError) as exc:
                raise SmartSubscriptionError("subscription is expired") from exc
            if int(subscription.get("traffic_bytes") or 0) <= int(
                subscription.get("usage_bytes") or 0
            ):
                raise SmartSubscriptionError("subscription is expired")

            mappings = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM tenant_subscription_nodes "
                    "WHERE tenant_id=? AND subscription_id=? "
                    "AND status='active' AND external_ref IS NOT NULL "
                    "ORDER BY is_primary DESC, id ASC",
                    (tenant_id, subscription_id),
                ).fetchall()
            ]
            if not mappings and subscription.get("server_id") and subscription.get(
                "external_ref"
            ):
                mappings = [
                    {
                        "server_id": int(subscription["server_id"]),
                        "external_ref": str(subscription["external_ref"]),
                        "is_primary": 1,
                    }
                ]
            if not mappings:
                raise SmartSubscriptionError("subscription has no active nodes")

            business = TenantBusinessService(
                conn,
                tenant_id=tenant_id,
                owner_telegram_id=int(subscription["owner_telegram_id"]),
                secret_cipher=self.cipher,
                panel_adapter=adapter,
            )
            payloads: list[str] = []
            for mapping in mappings:
                secret = ""
                try:
                    _, target, secret = business._panel_material(
                        int(mapping["server_id"])
                    )
                    payload = adapter.subscription_content(
                        target=target,
                        secret=secret,
                        external_ref=str(mapping["external_ref"]),
                    )
                    if payload:
                        payloads.append(payload)
                except (PanelError, Exception) as exc:
                    # One unavailable child node must not take down a healthy
                    # multi-node subscription. Do not expose provider details.
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    continue
                finally:
                    secret = ""

            lines = aggregate_lines(payloads)
            if not lines:
                raise SmartSubscriptionError("subscription is empty")
            plain = "\n".join(lines)
            body = (
                base64.b64encode(plain.encode("utf-8")).decode("ascii")
                if base64_output
                else plain
            )

            title = str(subscription.get("plan_name") or link["label"] or "subscription")
            title_b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
            total = max(0, int(subscription.get("traffic_bytes") or 0))
            used = max(0, int(subscription.get("usage_bytes") or 0))
            expire = int(parse_utc(str(subscription["expires_at"])).timestamp())
            headers = {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "profile-title": f"base64:{title_b64}",
                "profile-update-interval": "24",
                "subscription-userinfo": (
                    f"upload=0; download={used}; total={total}; expire={expire}"
                ),
            }
            return SmartSubscriptionResponse(body=body, headers=headers)
        finally:
            conn.close()


class SmartSubscriptionHTTPServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        service: SmartSubscriptionService,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.service = service
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        service = self.service

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def _write(
                self, status: int, body: str, headers: dict[str, str] | None = None
            ) -> None:
                encoded = body.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("X-Frame-Options", "DENY")
                for key, value in (headers or {}).items():
                    self.send_header(str(key), str(value))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) not in (2, 3) or parts[0] != "sub":
                    self._write(404, "not found")
                    return
                code = parts[1]
                filename = parts[2] if len(parts) == 3 else "all.b64"
                query = parse_qs(parsed.query)
                if filename not in {"all.txt", "all.b64"}:
                    self._write(404, "not found")
                    return
                base64_output = filename == "all.b64" or (
                    (query.get("base64") or ["0"])[0] == "1"
                )
                try:
                    result = service.build(code, base64_output=base64_output)
                except SmartSubscriptionError as exc:
                    message = str(exc)
                    status = 410 if "expired" in message or "not active" in message else 404
                    self._write(status, message, {"Cache-Control": "no-store"})
                    return
                self._write(200, result.body, result.headers)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="whitelabel-smart-sub",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
