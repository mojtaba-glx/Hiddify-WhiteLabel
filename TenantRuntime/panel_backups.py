"""Native panel backup downloads; never synthesize a backup from a user list."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, quote

import httpx

from TenantRuntime.panels import PanelError

MAX_PANEL_BACKUP_BYTES = 40 * 1024 * 1024


@dataclass(frozen=True)
class PanelBackup:
    filename: str
    content: bytes


def safe_name(value: str, default: str = "backup") -> str:
    name = str(value or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w. @()-]+", "_", name).strip(" .")[:160]
    return name or default


def response_filename(response: httpx.Response, default: str) -> str:
    header = Message()
    header["Content-Disposition"] = response.headers.get("content-disposition", "")
    return safe_name(header.get_filename() or default, default)


def bounded_request(
    client: httpx.Client, method: str, url: str, **kwargs
) -> httpx.Response:
    """Bound decompressed bytes as well as Content-Length before buffering."""
    kwargs.setdefault("timeout", 60.0)
    try:
        with client.stream(method, url, follow_redirects=False, **kwargs) as response:
            if response.status_code >= 300:
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    request=response.request,
                )
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_PANEL_BACKUP_BYTES:
                raise PanelError("panel backup exceeds size limit")
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_PANEL_BACKUP_BYTES:
                    raise PanelError("panel backup exceeds size limit")
                body.extend(chunk)
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )
    except httpx.TransportError as exc:
        raise PanelError("panel backup connection failed") from exc


def _hiddify_artifact(response: httpx.Response) -> PanelBackup | None:
    if response.status_code != 200 or not response.content:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    # Reject JSON API errors/acknowledgements as well as HTML login pages.
    if (
        not isinstance(data, dict)
        or not any(key in data for key in ("users", "hconfigs", "configs", "domains"))
        or data.get("success") is False
        or data.get("error")
    ):
        return None
    filename = response_filename(response, "hiddify-backup.json")
    if not filename.lower().endswith(".json"):
        filename += ".json"
    return PanelBackup(filename, response.content)


class _BackupPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "form":
            self.current = {
                "action": attrs.get("action", ""),
                "method": attrs.get("method", "get").upper(),
                "data": {},
            }
        elif tag == "input" and self.current is not None and attrs.get("name"):
            if attrs.get("type", "").lower() in {"hidden", "submit"}:
                self.current["data"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def _safe_download_url(url: str, admin_base: str) -> bool:
    parsed, base = urlsplit(url), urlsplit(admin_base)
    path = unquote(parsed.path).lower()
    return (
        (parsed.scheme, parsed.netloc) == (base.scheme, base.netloc)
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith(base.path.rstrip("/") + "/")
        and not any(
            x in path + "?" + parsed.query.lower()
            for x in ("..", "delete", "remove", "restore", "upload", "logout")
        )
        and any(x in path for x in ("backupfile", "download", ".json"))
    )


def download_hiddify(adapter, target, secret: str) -> PanelBackup:
    from TenantRuntime.hiddify import (
        _admin_base,
        _ssl_mode,
        _insecure_context,
        _looks_like_tls_error,
    )

    base = _admin_base(target)
    mode = _ssl_mode()
    options = [True] if mode == "secure" else [_insecure_context()]
    if mode == "auto":
        options = [True, _insecure_context()]
    for index, verify in enumerate(options):
        try:
            with httpx.Client(
                transport=adapter._transport,
                verify=verify,
                timeout=60.0,
                follow_redirects=False,
            ) as client:
                headers = {
                    "Hiddify-API-Key": secret,
                    "Accept": "application/json,text/html",
                }
                pages = []

                def fetch(url, method="GET", data=None):
                    for _ in range(4):
                        response = bounded_request(
                            client,
                            method,
                            url,
                            headers=headers,
                            **(
                                {"data": data} if method == "POST" else {"params": data}
                            ),
                        )
                        if response.status_code not in {301, 302, 303, 307, 308}:
                            return response
                        redirected = urljoin(url, response.headers.get("location", ""))
                        parsed, original = urlsplit(redirected), urlsplit(base)
                        if (parsed.scheme, parsed.netloc) != (
                            original.scheme,
                            original.netloc,
                        ) or not parsed.path.startswith(
                            original.path.rstrip("/") + "/"
                        ):
                            raise PanelError("unsafe panel backup redirect")
                        url = redirected
                        if response.status_code == 303:
                            method, data = "GET", None
                    raise PanelError("panel backup redirect limit exceeded")

                for path in (
                    "admin/backup/backupfile",
                    "admin/backup/backupfile/",
                    "backup",
                    "admin/backup",
                    "api/v2/admin/backup/",
                    "api/v2/admin/backup",
                    "api/v2/admin/user/backup/",
                    "api/v2/admin/user/backup",
                ):
                    response = fetch(f"{base}/{path}")
                    artifact = _hiddify_artifact(response)
                    if artifact:
                        return artifact
                    if response.status_code in {401, 403}:
                        continue
                    if response.status_code == 200 and path == "admin/backup":
                        pages.append(response)
                for page in pages:
                    parser = _BackupPage()
                    parser.feed(page.text)
                    for href in parser.links[:30]:
                        url = urljoin(str(page.url), href)
                        if _safe_download_url(url, base):
                            artifact = _hiddify_artifact(fetch(url))
                            if artifact:
                                return artifact
                    for form in parser.forms[:10]:
                        url = urljoin(str(page.url), form["action"])
                        if _safe_download_url(url, base) and form["method"] in {
                            "GET",
                            "POST",
                        }:
                            artifact = _hiddify_artifact(
                                fetch(url, form["method"], form["data"])
                            )
                            if artifact:
                                return artifact
            raise PanelError("Hiddify backup download failed")
        except PanelError as exc:
            if (
                mode == "auto"
                and index == 0
                and exc.__cause__
                and _looks_like_tls_error(exc.__cause__)
            ):
                continue
            raise
    raise PanelError("Hiddify backup download failed")


def download_xui(adapter, target, secret: str) -> PanelBackup:
    from TenantRuntime.xui import _SECRET_HEADER

    with adapter._session(target, secret) as session:
        headers = {"Accept": "application/octet-stream"}
        if session.token_auth:
            headers["Authorization"] = f"Bearer {session.credential['api_token']}"
        elif session.credential.get("secret_header"):
            headers[_SECRET_HEADER] = session.credential["secret_header"]
        response = bounded_request(
            session.client, "GET", f"{session.api_base}/server/getDb", headers=headers
        )
        if response.status_code != 200 or not response.content.startswith(
            b"SQLite format 3\x00"
        ):
            raise PanelError("X-UI returned an invalid database backup")
        return PanelBackup(
            response_filename(response, "xui-backup.db"), response.content
        )


def download_xnet(adapter, target, secret: str) -> PanelBackup:
    def identity(meta):
        return next(
            (
                str(meta[k])
                for k in ("id", "backupId", "backup_id")
                if isinstance(meta, dict) and meta.get(k)
            ),
            "",
        )

    def inventory(data):
        if isinstance(data, dict):
            data = next(
                (
                    data[k]
                    for k in ("data", "backups", "items", "result")
                    if isinstance(data.get(k), list)
                ),
                [],
            )
        return (
            {identity(row): row for row in data if identity(row)}
            if isinstance(data, list)
            else {}
        )

    with adapter._session(target, secret) as session:
        try:
            before = inventory(session.request("GET", "/api/backups"))
        except PanelError:
            before = None
        created = session.request("POST", "/api/backups")
        meta = created
        if isinstance(meta, dict):
            for key in ("data", "backup", "result"):
                if isinstance(meta.get(key), dict):
                    meta = meta[key]
                    break
        backup_id = identity(meta)
        direct_identity = bool(backup_id)
        # Some versions acknowledge creation without an id. Compare inventories
        # instead of assuming that an old backup is the one just created.
        if not backup_id and before is not None:
            after = inventory(session.request("GET", "/api/backups"))
            new_ids = after.keys() - before.keys()
            if len(new_ids) == 1:
                backup_id = new_ids.pop()
                meta = after[backup_id]
        if not backup_id:
            raise PanelError("X-NET returned no new backup identity")
        path = f"/api/backups/{quote(backup_id, safe='')}"
        response = session.request("GET", path + "/download", raw=True)
        body = response.content
        if response.status_code != 200 or not body.startswith(
            (b"PK\x03\x04", b"\x1f\x8b", b"SQLite format 3\x00")
        ):
            raise PanelError("X-NET returned an invalid backup archive")
        default = safe_name(
            meta.get("filename")
            or meta.get("fileName")
            or meta.get("name")
            or "xnet-backup.zip"
        )
        artifact = PanelBackup(response_filename(response, default), body)
        if direct_identity:
            try:
                session.request("DELETE", path)
            except PanelError:
                pass
        return artifact
