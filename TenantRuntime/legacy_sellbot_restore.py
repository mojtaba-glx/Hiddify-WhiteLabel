"""Import a real Hiddify-SellBot Backup_All ZIP into one WhiteLabel Tenant.

SellBot restores its native databases/files in place.  WhiteLabel cannot copy
those process-wide files because its database is shared by multiple tenants.
Instead this importer performs the same preflight discipline, converts the
operational UserBot/AdminBot state into native tenant_* rows, and preserves all
source members tenant-scoped for lossless future migration of AgentBot /
CustomerBot history.

PanelBackups are preserved as source assets.  Matching SellBot's own
_restore_from_zip_backup, this importer does not push panel DB/config backups
back into live panels automatically.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from Shared.crypto import TokenCipher, TokenCipherError, fingerprint_token
from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.backup import (
    BACKUP_FORMAT,
    TenantBackupError,
    restore_tenant_backup,
    tenant_backup_tables,
)
from TenantRuntime.server_connections import credential


LEGACY_FORMAT = "hiddify-sellbot-full-v1"
MAX_LEGACY_ZIP_BYTES = 256 * 1024 * 1024
MAX_LEGACY_MEMBER_BYTES = 128 * 1024 * 1024
MAX_LEGACY_MEMBERS = 4096
_REQUIRED_MAIN_DB = (
    "Shared/hiddify_sellbot.db",
    "Shared/userbot.db",
    "hiddify_sellbot.db",
    "userbot.db",
)
_REQUIRED_TABLES = {
    "userbot_users",
    "userbot_services",
    "userbot_orders",
    "userbot_settings",
}


@dataclass(frozen=True)
class LegacySellBotRestoreReport:
    tenant_id: int
    format: str
    tables_restored: int
    rows_restored: int
    table_names: tuple[str, ...]
    source_members: int
    assets_preserved: int
    users: int
    orders: int
    services: int
    tickets: int
    panel_backups: int
    warnings: tuple[str, ...]


class LegacySellBotRestoreError(TenantBackupError):
    pass


def _qid(name: str) -> str:
    value = str(name or "")
    if not value or "\x00" in value:
        raise LegacySellBotRestoreError("invalid sqlite identifier")
    return '"' + value.replace('"', '""') + '"'


def _safe_member_name(name: str) -> str:
    value = str(name or "").replace("\\", "/").strip()
    if not value or value.startswith("/") or "\x00" in value:
        raise LegacySellBotRestoreError("unsafe backup member path")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise LegacySellBotRestoreError("unsafe backup member path")
    return "/".join(parts)


def _zip_index(data: bytes) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    raw = bytes(data)
    if len(raw) > MAX_LEGACY_ZIP_BYTES:
        raise LegacySellBotRestoreError("SellBot backup exceeds size limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), mode="r")
    except zipfile.BadZipFile as exc:
        raise LegacySellBotRestoreError("SellBot backup ZIP is invalid") from exc
    infos = [info for info in zf.infolist() if not info.is_dir()]
    if not infos or len(infos) > MAX_LEGACY_MEMBERS:
        zf.close()
        raise LegacySellBotRestoreError("SellBot backup member count is invalid")
    index: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        if name in index:
            zf.close()
            raise LegacySellBotRestoreError("SellBot backup contains duplicate paths")
        if int(info.file_size or 0) > MAX_LEGACY_MEMBER_BYTES:
            zf.close()
            raise LegacySellBotRestoreError("SellBot backup member is too large")
        total += int(info.file_size or 0)
        if total > MAX_LEGACY_ZIP_BYTES:
            zf.close()
            raise LegacySellBotRestoreError("SellBot backup expands beyond size limit")
        index[name] = info
    bad = zf.testzip()
    if bad:
        zf.close()
        raise LegacySellBotRestoreError("SellBot backup CRC check failed")
    return zf, index


def _member_bytes(
    zf: zipfile.ZipFile,
    index: dict[str, zipfile.ZipInfo],
    name: str,
) -> bytes:
    info = index.get(name)
    if info is None:
        raise LegacySellBotRestoreError(f"missing SellBot backup member: {name}")
    raw = zf.read(info)
    if len(raw) != int(info.file_size or 0):
        raise LegacySellBotRestoreError("SellBot backup member size mismatch")
    return raw


def _json_member(
    zf: zipfile.ZipFile,
    index: dict[str, zipfile.ZipInfo],
    name: str,
) -> dict[str, Any]:
    try:
        obj = json.loads(_member_bytes(zf, index, name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacySellBotRestoreError(
            f"SellBot JSON member is invalid: {name}"
        ) from exc
    if not isinstance(obj, dict):
        raise LegacySellBotRestoreError(f"SellBot JSON object expected: {name}")
    return obj


def _find_one(index: dict[str, zipfile.ZipInfo], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in index:
            return candidate
    return ""


def _find_manifest(index: dict[str, zipfile.ZipInfo], prefix: str) -> str:
    names = sorted(
        name
        for name in index
        if name.rsplit("/", 1)[-1].startswith(prefix)
        and name.lower().endswith(".json")
    )
    return names[-1] if names else ""


def _verify_bot_manifest(
    zf: zipfile.ZipFile,
    index: dict[str, zipfile.ZipInfo],
) -> str:
    name = _find_manifest(index, "Backup_Bot_")
    if not name:
        return ""
    manifest = _json_member(zf, index, name)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise LegacySellBotRestoreError("SellBot bot manifest has no files list")
    expected_count = int(manifest.get("files_count") or 0)
    if expected_count and expected_count != len(files):
        raise LegacySellBotRestoreError("SellBot bot manifest count mismatch")
    for item in files:
        if not isinstance(item, dict):
            raise LegacySellBotRestoreError("SellBot bot manifest entry is invalid")
        path = _safe_member_name(str(item.get("path") or ""))
        raw = _member_bytes(zf, index, path)
        expected_size = int(item.get("size") or 0)
        if expected_size != len(raw):
            raise LegacySellBotRestoreError(
                f"SellBot manifest size mismatch: {path}"
            )
        expected_sha = str(item.get("sha256") or "").strip().lower()
        if expected_sha:
            actual = hashlib.sha256(raw).hexdigest()
            if expected_sha != actual:
                raise LegacySellBotRestoreError(
                    f"SellBot manifest checksum mismatch: {path}"
                )
    return name


def _sqlite_payload(raw: bytes, *, label: str) -> dict[str, list[dict[str, Any]]]:
    fd, path = tempfile.mkstemp(prefix="wl-sellbot-restore-", suffix=".db")
    os.close(fd)
    try:
        with open(path, "wb") as handle:
            handle.write(raw)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).strip().lower() != "ok":
                raise LegacySellBotRestoreError(
                    f"SellBot SQLite integrity failed: {label}"
                )
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            result: dict[str, list[dict[str, Any]]] = {}
            for table in tables:
                result[table] = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {_qid(table)}"
                    ).fetchall()
                ]
            return result
        finally:
            conn.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _preflight_sellbot_backup(data: bytes) -> dict[str, Any]:
    zf, index = _zip_index(data)
    try:
        bot_manifest = _verify_bot_manifest(zf, index)
        main_db_name = _find_one(index, _REQUIRED_MAIN_DB)
        if not main_db_name:
            raise LegacySellBotRestoreError(
                "SellBot main database is missing from backup"
            )
        main_db = _sqlite_payload(
            _member_bytes(zf, index, main_db_name),
            label=main_db_name,
        )
        missing = _REQUIRED_TABLES - set(main_db)
        if missing:
            raise LegacySellBotRestoreError(
                "SellBot main database is missing required tables"
            )

        for candidate in (
            "Shared/agency.db",
            "agency.db",
            "customer_bot.db",
            "CustomerBot/customer_bot.db",
            "AgentBot/agent_bot.db",
            "agent_bot.db",
        ):
            if candidate in index:
                _sqlite_payload(
                    _member_bytes(zf, index, candidate),
                    label=candidate,
                )

        servers_name = _find_one(
            index, ("Shared/servers.json", "servers.json")
        )
        plans_name = _find_one(index, ("Shared/plans.json", "plans.json"))
        servers = (
            _json_member(zf, index, servers_name)
            if servers_name else {"servers": [], "settings": {}}
        )
        plans = (
            _json_member(zf, index, plans_name)
            if plans_name else {"servers": {}}
        )
        if not isinstance(servers.get("servers", []), list):
            raise LegacySellBotRestoreError("SellBot servers.json is invalid")
        if not isinstance(plans.get("servers", {}), dict):
            raise LegacySellBotRestoreError("SellBot plans.json is invalid")

        members = {
            name: _member_bytes(zf, index, name)
            for name in sorted(index)
        }
        full_manifest = _find_manifest(index, "Backup_All_")
        return {
            "index": tuple(sorted(index)),
            "members": members,
            "main_db_name": main_db_name,
            "main_db": main_db,
            "servers": servers,
            "plans": plans,
            "bot_manifest": bot_manifest,
            "full_manifest": full_manifest,
        }
    finally:
        zf.close()


def is_sellbot_backup(data: bytes) -> bool:
    try:
        zf, index = _zip_index(data)
    except LegacySellBotRestoreError:
        return False
    try:
        return bool(
            _find_one(index, _REQUIRED_MAIN_DB)
            and (
                _find_manifest(index, "Backup_Bot_")
                or "Shared/servers.json" in index
                or "Shared/plans.json" in index
            )
        )
    finally:
        zf.close()


class _Ids:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.next: dict[str, int] = {}

    def one(self, table: str) -> int:
        if table not in self.next:
            row = self.conn.execute(
                f"SELECT COALESCE(MAX(id),0)+1 AS n FROM {_qid(table)}"
            ).fetchone()
            self.next[table] = int(row["n"] if row is not None else 1)
        value = self.next[table]
        self.next[table] = value + 1
        return value


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        str(row["name"]): row
        for row in conn.execute(
            f"PRAGMA table_info({_qid(table)})"
        ).fetchall()
    }


def _fit_row(
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    columns = _columns(conn, table)
    result = {key: value for key, value in row.items() if key in columns}
    for name, info in columns.items():
        if name in result:
            continue
        if int(info["pk"] or 0) > 0:
            raise LegacySellBotRestoreError(
                f"missing target primary key: {table}.{name}"
            )
        if int(info["notnull"] or 0) and info["dflt_value"] is None:
            raise LegacySellBotRestoreError(
                f"missing target required column: {table}.{name}"
            )
    return result


def _blob_marker(raw: bytes) -> dict[str, str]:
    return {"__wl_bytes_b64__": base64.b64encode(bytes(raw)).decode("ascii")}


def _text(value: Any, limit: int, default: str = "") -> str:
    value = str(value or "").strip()
    return (value or default)[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _legacy_comment_value(value: Any, key: str) -> str:
    wanted = str(key or "").strip().lower()
    if not wanted:
        return ""
    for part in str(value or "").split("|"):
        if ":" not in part:
            continue
        raw_key, raw_value = part.split(":", 1)
        if raw_key.strip().lower() == wanted:
            return raw_value.strip()
    return ""


def _iso(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _backup_created_at(preflight: dict[str, Any], fallback: datetime) -> datetime:
    for manifest_name in (
        str(preflight.get("full_manifest") or ""),
        str(preflight.get("bot_manifest") or ""),
    ):
        if not manifest_name:
            continue
        try:
            obj = json.loads(
                preflight["members"][manifest_name].decode("utf-8")
            )
            raw = str(obj.get("created_at") or "").strip()
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            continue
    return fallback.astimezone(timezone.utc)


def _origin(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    return "https://" + raw.strip("/")


def _unique_name(used: set[str], value: str, *, limit: int = 80) -> str:
    base = _text(value, limit, "Legacy")
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f" ({suffix})"
        candidate = base[: max(1, limit - len(tail))] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _map_userbot_settings(
    legacy: dict[str, list[dict[str, Any]]],
    *,
    tenant_id: int,
    now: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from TenantRuntime.business import USERBOT_SETTING_DEFAULTS

    raw_settings: dict[str, Any] = {}
    for row in legacy.get("userbot_settings", []):
        key = str(row.get("key") or "").strip()
        value = row.get("value")
        if not key:
            continue
        try:
            raw_settings[key] = json.loads(str(value))
        except Exception:
            raw_settings[key] = value

    merged: dict[str, Any] = {}

    def merge_group(name: str) -> None:
        value = raw_settings.get(name)
        if isinstance(value, dict):
            for key, item in value.items():
                if key in USERBOT_SETTING_DEFAULTS:
                    merged[key] = item

    for name in (
        "buy_renew_settings",
        "subscription_settings",
        "text_settings",
        "marketing_settings",
        "ui_settings",
        "tx_plans_settings",
    ):
        merge_group(name)

    force = raw_settings.get("force_join_settings")
    if isinstance(force, dict):
        merged["force_join_enabled"] = bool(force.get("enabled"))
        merged["force_join_channel_id"] = str(force.get("channel_id") or "")
        merged["force_join_channel_username"] = str(
            force.get("channel_username") or ""
        ).lstrip("@")
        merged["force_join_channel_link"] = str(force.get("channel_link") or "")
        merged["force_join_guide_text"] = str(force.get("guide_text") or "")

    payment = raw_settings.get("payment_settings")
    if isinstance(payment, dict):
        merged["payment_event_channel_enabled"] = bool(
            payment.get("event_channel_enabled")
        )
        merged["payment_event_channel_id"] = str(
            payment.get("event_channel_id") or ""
        )

    managed = str(raw_settings.get("managed_sub_base_url") or "").strip()
    if managed:
        merged["smart_base_url"] = managed.rstrip("/")

    rows: list[dict[str, Any]] = []
    for key, value in sorted(merged.items()):
        if key not in USERBOT_SETTING_DEFAULTS:
            continue
        rows.append(
            {
                "tenant_id": int(tenant_id),
                "key": key,
                "value": json.dumps(value, ensure_ascii=False),
                "updated_at": now,
            }
        )

    growth: dict[str, Any] = {}
    trial = raw_settings.get("trial_spec_settings")
    if isinstance(trial, dict):
        growth.update(
            trial_enabled=1 if trial.get("enabled") else 0,
            trial_traffic_gb=max(1, int(math.ceil(_float(trial.get("usage_gb"), 1)))),
            trial_duration_days=max(1, _int(trial.get("days"), 1)),
            trial_announce_enabled=1 if trial.get("announce_enabled", True) else 0,
        )
    referral = raw_settings.get("referral_settings")
    if isinstance(referral, dict):
        growth.update(
            referral_enabled=1 if referral.get("referral_enabled") else 0,
            referral_trial_reward=max(0, _int(referral.get("trial_reward_amount"), 0)),
            referral_purchase_reward=max(0, _int(referral.get("purchase_reward_amount"), 0)),
            referral_min_purchase=max(0, _int(referral.get("min_purchase_amount"), 0)),
            referral_max_rewards=max(0, _int(referral.get("max_successful_referrals"), 0)),
            referral_trial_reward_enabled=1 if referral.get("trial_reward_enabled", True) else 0,
            referral_purchase_reward_enabled=1 if referral.get("purchase_reward_enabled", True) else 0,
            referral_invite_text=str(referral.get("invite_intro_text") or ""),
        )
    return rows, growth


def _encrypt_legacy_asset(cipher: TokenCipher, raw: bytes) -> bytes:
    """Encrypt arbitrary backup bytes using bounded generic-secret chunks."""
    chunks: list[str] = []
    data = bytes(raw)
    for offset in range(0, len(data), 2048):
        encoded = base64.b64encode(data[offset: offset + 2048]).decode("ascii")
        chunks.append(cipher.encrypt_secret(encoded))
    payload = {
        "version": 1,
        "encoding": "fernet-chunked-b64",
        "chunks": chunks,
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decrypt_legacy_asset(cipher: TokenCipher, content: bytes) -> bytes:
    """Future migration seam for preserved SellBot source members."""
    try:
        payload = json.loads(bytes(content).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacySellBotRestoreError("legacy asset envelope is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("encoding") != "fernet-chunked-b64"
        or not isinstance(payload.get("chunks"), list)
    ):
        raise LegacySellBotRestoreError("legacy asset envelope is unsupported")
    out = bytearray()
    for token in payload["chunks"]:
        try:
            decoded = cipher.decrypt_secret(str(token))
            out.extend(base64.b64decode(decoded.encode("ascii"), validate=True))
        except Exception as exc:
            raise LegacySellBotRestoreError(
                "legacy asset decryption failed"
            ) from exc
    return bytes(out)


def _legacy_asset_kind(path: str) -> str:
    low = path.lower()
    if path.startswith("PanelBackups/"):
        return "panel_backup"
    if path.startswith("Receiptions/"):
        return "media"
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return "database"
    if low.endswith(".json"):
        return (
            "manifest"
            if path.rsplit("/", 1)[-1].startswith(("Backup_Bot_", "Backup_All_"))
            else "json"
        )
    return "other"


def _build_snapshot(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    owner_telegram_id: int,
    cipher: TokenCipher,
    data: bytes,
    preflight: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int], list[str], str]:
    tid = int(tenant_id)
    now_dt = utcnow()
    now = iso_utc(now_dt)
    backup_dt = _backup_created_at(preflight, now_dt)
    backup_time = iso_utc(backup_dt)
    source_sha = hashlib.sha256(bytes(data)).hexdigest()
    restore_id = (
        "sellbot-"
        + now_dt.strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + source_sha[:12]
    )

    tables: dict[str, list[dict[str, Any]]] = {
        table: [] for table in tenant_backup_tables(conn)
    }
    ids = _Ids(conn)
    warnings: list[str] = []
    counts = {
        "users": 0,
        "orders": 0,
        "services": 0,
        "tickets": 0,
        "panel_backups": sum(
            1 for name in preflight["index"] if name.startswith("PanelBackups/")
        ),
        "assets": 0,
    }
    legacy = preflight["main_db"]

    # ---------- Servers + encrypted panel credentials ----------
    server_map: dict[int, int] = {}
    server_by_title: dict[str, int] = {}
    used_server_labels: set[str] = set()
    seen_secret_fingerprints: set[str] = set()
    legacy_servers = preflight["servers"].get("servers") or []
    for position, item in enumerate(legacy_servers):
        if not isinstance(item, dict):
            continue
        old_id = _int(item.get("id"), position + 1)
        new_id = ids.one("tenant_servers")
        label = _unique_name(
            used_server_labels,
            _text(item.get("title"), 80, f"Legacy Server {old_id}"),
        )
        raw_kind = str(
            item.get("panel_type")
            or item.get("provider_kind")
            or "hiddify"
        ).strip().lower()
        kind = raw_kind if raw_kind in {"hiddify", "xui", "xnet"} else "hiddify"
        legacy_kind = kind if kind in {"hiddify", "xui"} else "manual"
        flavor = ""
        if kind == "xui":
            flavor = str(item.get("xui_flavor") or "").strip().lower()
            if flavor not in {"sanaei", "alireza"}:
                flavor = (
                    "sanaei"
                    if str(item.get("xui_api_token") or item.get("xui_token") or "").strip()
                    else "alireza"
                )
        row = {
            "id": new_id,
            "tenant_id": tid,
            "label": label,
            "panel_kind": legacy_kind,
            "provider_kind": kind,
            "endpoint": str(item.get("panel_url") or "").strip() or None,
            "status": "active",
            "admin_path": str(item.get("admin_proxy_path") or "").strip() or None,
            "user_path": str(item.get("user_proxy_path") or "").strip() or None,
            "xui_flavor": flavor or None,
            "xui_inbound_ids": str(
                item.get("xui_inbound_ids")
                or item.get("xui_inbound_id")
                or ""
            ).strip() or None,
            "xui_public_origin": _origin(
                item.get("xui_public_origin")
                or item.get("xui_sub_domain")
            ) or None,
            "xui_sub_path": str(item.get("xui_sub_path") or "/sub/").strip() if kind == "xui" else None,
            "xnet_inbound_ids": str(item.get("xnet_inbound_ids") or "").strip() or None,
            "xnet_public_origin": _origin(
                item.get("xnet_public_origin")
                or item.get("xnet_sub_domain")
            ) or None,
            "xnet_sub_port": _int(item.get("xnet_sub_port"), 0) or None,
            "xnet_sub_path": str(item.get("xnet_sub_path") or "sub").strip() if kind == "xnet" else None,
            "xnet_api_url": str(item.get("xnet_api_url") or "").strip() or None,
            "users_limit": max(0, _int(item.get("users_limit"), 0)),
            "priority": max(0, _int(item.get("priority"), 0)),
            "is_default": 1 if position == 0 else 0,
            "created_at": backup_time,
            "updated_at": now,
        }
        tables["tenant_servers"].append(_fit_row(conn, "tenant_servers", row))
        server_map[old_id] = new_id
        server_by_title[str(item.get("title") or "").strip()] = new_id

        secret = ""
        try:
            if kind == "hiddify":
                secret = credential(
                    "hiddify",
                    values={"secret": str(item.get("admin_uuid") or "").strip()},
                )
            elif kind == "xui":
                secret = credential(
                    "xui",
                    flavor=flavor,
                    values={
                        "api_token": str(
                            item.get("xui_api_token")
                            or item.get("xui_token")
                            or ""
                        ).strip(),
                        "username": str(item.get("xui_username") or "").strip(),
                        "password": str(item.get("xui_password") or ""),
                        "secret_header": str(item.get("xui_secret_header") or "").strip(),
                    },
                )
            else:
                secret = credential(
                    "xnet",
                    values={
                        "api_token": str(item.get("xnet_api_token") or "").strip(),
                        "username": str(item.get("xnet_username") or "admin").strip(),
                        "password": str(item.get("xnet_password") or ""),
                    },
                )
        except Exception:
            if str(item.get("panel_url") or "").strip():
                warnings.append(
                    f"اطلاعات دسترسی سرور «{label}» قابل تبدیل نبود و در فایل خام بکاپ حفظ شد."
                )
        if secret:
            try:
                digest = fingerprint_token(secret)
                if digest in seen_secret_fingerprints:
                    warnings.append(
                        f"اطلاعات دسترسی تکراری سرور «{label}» برای جلوگیری از تداخل فعال نشد."
                    )
                else:
                    encrypted = cipher.encrypt_secret(secret)
                    seen_secret_fingerprints.add(digest)
                    tables["tenant_panel_credentials"].append(
                        _fit_row(
                            conn,
                            "tenant_panel_credentials",
                            {
                                "server_id": new_id,
                                "tenant_id": tid,
                                "encrypted_secret": encrypted,
                                "secret_fingerprint": digest,
                                "created_at": now,
                                "updated_at": now,
                            },
                        )
                    )
            except TokenCipherError:
                warnings.append(
                    f"رمزنگاری دسترسی سرور «{label}» انجام نشد؛ داده خام محفوظ است."
                )
            finally:
                secret = ""

        domains = item.get("domains")
        if isinstance(domains, list):
            primary_written = False
            for domain in domains:
                origin = _origin(domain)
                if not origin:
                    continue
                tables["tenant_server_domains"].append(
                    _fit_row(
                        conn,
                        "tenant_server_domains",
                        {
                            "id": ids.one("tenant_server_domains"),
                            "tenant_id": tid,
                            "server_id": new_id,
                            "title": urlsplit(origin).hostname or origin,
                            "origin": origin,
                            "is_primary": 0 if primary_written else 1,
                        },
                    )
                )
                primary_written = True

    # ---------- Node topology ----------
    node_pairs: set[tuple[int, int]] = set()
    for item in legacy_servers:
        if not isinstance(item, dict):
            continue
        parent_old = _int(item.get("id"), 0)
        parent_new = server_map.get(parent_old)
        if not parent_new:
            continue
        nodes = item.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            child_old = _int(node.get("target_server_id"), 0)
            child_new = server_map.get(child_old)
            if not child_new or child_new == parent_new:
                continue
            pair = (parent_new, child_new)
            if pair in node_pairs:
                continue
            node_pairs.add(pair)
            state = str(node.get("status") or "up").strip().lower()
            tables["tenant_nodes"].append(
                _fit_row(
                    conn,
                    "tenant_nodes",
                    {
                        "id": ids.one("tenant_nodes"),
                        "tenant_id": tid,
                        "server_id": child_new,
                        "parent_server_id": parent_new,
                        "label": _text(
                            node.get("title"),
                            80,
                            next(
                                (
                                    str(s.get("title") or "")
                                    for s in legacy_servers
                                    if _int(s.get("id"), 0) == child_old
                                ),
                                f"Node {child_old}",
                            ),
                        ),
                        "location": _text(node.get("title"), 80) or None,
                        "status": "active" if state in {"up", "online", "active"} else "offline",
                        "created_at": backup_time,
                        "updated_at": now,
                    },
                )
            )

    # Fallback relation stored directly on a legacy child server.
    for item in legacy_servers:
        if not isinstance(item, dict) or not item.get("is_node"):
            continue
        child_new = server_map.get(_int(item.get("id"), 0))
        parent_new = server_map.get(_int(item.get("parent_server_id"), 0))
        if not child_new or not parent_new or (parent_new, child_new) in node_pairs:
            continue
        node_pairs.add((parent_new, child_new))
        tables["tenant_nodes"].append(
            _fit_row(
                conn,
                "tenant_nodes",
                {
                    "id": ids.one("tenant_nodes"),
                    "tenant_id": tid,
                    "server_id": child_new,
                    "parent_server_id": parent_new,
                    "label": _text(item.get("title"), 80, "Node"),
                    "location": _text(item.get("title"), 80) or None,
                    "status": "active",
                    "created_at": backup_time,
                    "updated_at": now,
                },
            )
        )

    # ---------- Catalog ----------
    used_categories: set[str] = set()
    used_plans: set[str] = set()
    plan_cache: dict[tuple[Any, ...], int] = {}
    legacy_plans = preflight["plans"].get("servers") or {}
    if isinstance(legacy_plans, dict):
        for server_key, spec in legacy_plans.items():
            if not isinstance(spec, dict):
                continue
            old_server = _int(server_key, 0)
            new_server = server_map.get(old_server)
            category_map: dict[int, int] = {}
            for cat in spec.get("categories") or []:
                if not isinstance(cat, dict):
                    continue
                cid = ids.one("tenant_plan_categories")
                title = _unique_name(
                    used_categories,
                    _text(cat.get("title"), 80, "Legacy"),
                )
                category_map[_int(cat.get("id"), 0)] = cid
                tables["tenant_plan_categories"].append(
                    _fit_row(
                        conn,
                        "tenant_plan_categories",
                        {
                            "id": cid,
                            "tenant_id": tid,
                            "title": title,
                            "priority": _int(cat.get("priority"), 0),
                            "status": "active",
                            "created_at": backup_time,
                            "updated_at": now,
                        },
                    )
                )
            for plan in spec.get("plans") or []:
                if not isinstance(plan, dict):
                    continue
                pid = ids.one("tenant_sale_plans")
                traffic = max(1, int(math.ceil(_float(plan.get("gb"), 1))))
                days = max(1, _int(plan.get("days"), 30))
                price = max(0, _int(plan.get("price"), 0))
                title = _unique_name(
                    used_plans,
                    _text(plan.get("title"), 80, f"Legacy Plan {pid}"),
                )
                row = {
                    "id": pid,
                    "tenant_id": tid,
                    "name": title,
                    "traffic_gb": traffic,
                    "duration_days": days,
                    "price": price,
                    "currency": "IRR",
                    "status": "active",
                    "category_id": category_map.get(_int(plan.get("category_id"), 0)),
                    "priority": _int(plan.get("priority"), 0),
                    "server_id": new_server,
                    "is_dynamic": 0,
                    "created_at": backup_time,
                    "updated_at": now,
                }
                tables["tenant_sale_plans"].append(
                    _fit_row(conn, "tenant_sale_plans", row)
                )
                plan_cache[
                    (old_server, str(plan.get("title") or "").strip(), traffic, days, price)
                ] = pid

            settings = {
                "mode": str(spec.get("display_mode") or "fixed").strip().lower(),
            }
            dynamic = spec.get("dynamic_settings")
            if isinstance(dynamic, dict):
                settings.update(dynamic)
            if new_server:
                tables["tenant_server_sales_settings"].append(
                    _fit_row(
                        conn,
                        "tenant_server_sales_settings",
                        {
                            "tenant_id": tid,
                            "server_id": new_server,
                            "settings_json": json.dumps(
                                settings, ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                    )
                )

    def ensure_plan(
        *,
        old_server: int,
        title: str,
        gb: float,
        days: int,
        price: int,
    ) -> int:
        traffic = max(1, int(math.ceil(max(0.001, gb))))
        duration = max(1, int(days or 30))
        amount = max(0, int(price or 0))
        raw_title = str(title or "").strip()
        candidates = [
            key for key in plan_cache
            if key[0] == old_server
            and key[1] == raw_title
            and key[2] == traffic
            and key[3] == duration
            and key[4] == amount
        ]
        if candidates:
            return plan_cache[candidates[0]]
        key = (old_server, raw_title, traffic, duration, amount)
        if key in plan_cache:
            return plan_cache[key]
        pid = ids.one("tenant_sale_plans")
        name = _unique_name(
            used_plans,
            _text(raw_title, 60, "Legacy") + " [بازیابی]",
        )
        tables["tenant_sale_plans"].append(
            _fit_row(
                conn,
                "tenant_sale_plans",
                {
                    "id": pid,
                    "tenant_id": tid,
                    "name": name,
                    "traffic_gb": traffic,
                    "duration_days": duration,
                    "price": amount,
                    "currency": "IRR",
                    "status": "archived",
                    "category_id": None,
                    "priority": 0,
                    "server_id": server_map.get(old_server),
                    "is_dynamic": 0,
                    "created_at": backup_time,
                    "updated_at": now,
                },
            )
        )
        plan_cache[key] = pid
        return pid

    # ---------- Customers / wallets ----------
    customer_map: dict[int, int] = {}
    telegram_customer: dict[int, int] = {}
    legacy_users = legacy.get("userbot_users", [])
    for old in legacy_users:
        old_id = _int(old.get("id"), 0)
        telegram_id = _int(old.get("telegram_id"), 0)
        if old_id <= 0 or telegram_id <= 0:
            continue
        new_id = ids.one("tenant_customers")
        customer_map[old_id] = new_id
        telegram_customer[telegram_id] = new_id
        created = _iso(old.get("created_at"), fallback=backup_time)
        referral_code = _text(old.get("referral_code"), 80) or None
        row = {
            "id": new_id,
            "tenant_id": tid,
            "telegram_user_id": telegram_id,
            "display_name": _text(
                old.get("full_name"),
                120,
                _text(old.get("username"), 64, str(telegram_id)),
            ),
            "username": _text(old.get("username"), 64) or None,
            "status": "blocked" if _int(old.get("is_banned"), 0) else "active",
            "referral_code": referral_code,
            "invited_by_customer_id": None,
            "trial_used_at": created if _int(old.get("got_free_trial"), 0) else None,
            "created_at": created,
            "updated_at": now,
        }
        tables["tenant_customers"].append(
            _fit_row(conn, "tenant_customers", row)
        )
        balance = max(0, _int(old.get("wallet_balance"), 0))
        tables["tenant_wallet_accounts"].append(
            _fit_row(
                conn,
                "tenant_wallet_accounts",
                {
                    "tenant_id": tid,
                    "customer_id": new_id,
                    "currency": "IRR",
                    "balance": balance,
                    "updated_at": now,
                },
            )
        )

    # Resolve inviter links after every customer exists.
    old_user_by_id = {
        _int(row.get("id"), 0): row for row in legacy_users
    }
    for row in tables["tenant_customers"]:
        old_id = next(
            (
                old
                for old, new in customer_map.items()
                if int(new) == int(row["id"])
            ),
            0,
        )
        source = old_user_by_id.get(old_id) or {}
        inviter = customer_map.get(_int(source.get("invited_by_user_id"), 0))
        if inviter:
            row["invited_by_customer_id"] = inviter

    counts["users"] = len(customer_map)

    # ---------- Payment methods ----------
    cards = (
        preflight["servers"].get("settings", {}).get("cards", [])
        if isinstance(preflight["servers"].get("settings"), dict)
        else []
    )
    used_method_titles: set[str] = set()
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        number = str(card.get("number") or "").strip()
        if not number:
            continue
        mid = ids.one("tenant_payment_methods")
        title = _unique_name(
            used_method_titles,
            _text(card.get("bank"), 60, "کارت بانکی"),
        )
        owner = _text(card.get("owner"), 120)
        tables["tenant_payment_methods"].append(
            _fit_row(
                conn,
                "tenant_payment_methods",
                {
                    "id": mid,
                    "tenant_id": tid,
                    "kind": "card",
                    "provider_key": "card_manual",
                    "title": title,
                    "currency": "IRR",
                    "destination": number,
                    "network": None,
                    "instructions": (
                        f"به نام {owner}" if owner else ""
                    ),
                    "priority": 100,
                    "provider_options_json": "{}",
                    "status": "active",
                    "created_at": backup_time,
                    "updated_at": now,
                },
            )
        )

    # ---------- UserBot settings / growth ----------
    settings_rows, growth = _map_userbot_settings(
        legacy, tenant_id=tid, now=now
    )
    tables["tenant_userbot_settings"].extend(
        _fit_row(conn, "tenant_userbot_settings", row)
        for row in settings_rows
    )
    if growth:
        growth_row = {
            "tenant_id": tid,
            "referral_enabled": int(growth.get("referral_enabled", 0)),
            "referral_trial_reward": int(growth.get("referral_trial_reward", 0)),
            "referral_purchase_reward": int(growth.get("referral_purchase_reward", 0)),
            "referral_min_purchase": int(growth.get("referral_min_purchase", 0)),
            "referral_max_rewards": int(growth.get("referral_max_rewards", 0)),
            "referral_currency": "IRR",
            "trial_enabled": int(growth.get("trial_enabled", 0)),
            "trial_traffic_gb": int(growth.get("trial_traffic_gb", 1)),
            "trial_duration_days": int(growth.get("trial_duration_days", 1)),
            "trial_announce_enabled": int(growth.get("trial_announce_enabled", 1)),
            "referral_trial_reward_enabled": int(
                growth.get("referral_trial_reward_enabled", 1)
            ),
            "referral_purchase_reward_enabled": int(
                growth.get("referral_purchase_reward_enabled", 1)
            ),
            "referral_invite_text": str(growth.get("referral_invite_text") or ""),
            "updated_at": now,
        }
        tables["tenant_sales_growth_settings"].append(
            _fit_row(conn, "tenant_sales_growth_settings", growth_row)
        )

    # ---------- Orders ----------
    order_map: dict[int, int] = {}
    order_meta: dict[int, dict[str, Any]] = {}
    used_order_ids_for_service: set[int] = set()
    legacy_orders = legacy.get("userbot_orders", [])
    for old in legacy_orders:
        old_id = _int(old.get("id"), 0)
        customer = customer_map.get(_int(old.get("user_id"), 0))
        if old_id <= 0 or not customer:
            continue
        old_server = 0
        location = str(old.get("server_location") or "").strip()
        for source in legacy_servers:
            if str(source.get("title") or "").strip() == location:
                old_server = _int(source.get("id"), 0)
                break
        plan_id = ensure_plan(
            old_server=old_server,
            title=str(old.get("plan_title") or ""),
            gb=_float(old.get("volume_gb"), 1),
            days=max(1, _int(old.get("days"), 30)),
            price=max(0, _int(old.get("price"), 0)),
        )
        new_id = ids.one("tenant_orders")
        order_map[old_id] = new_id
        created = _iso(old.get("created_at"), fallback=backup_time)
        legacy_status = str(old.get("status") or "").strip().lower()
        status = (
            "fulfilled"
            if legacy_status in {"approved", "paid", "fulfilled", "complete", "completed"}
            else "rejected"
            if legacy_status in {"rejected", "failed"}
            else "pending_payment"
        )
        renewal = _int(old.get("renew_service_id"), 0) > 0
        amount = max(0, _int(old.get("price"), 0))
        row = {
            "id": new_id,
            "tenant_id": tid,
            "customer_id": customer,
            "plan_id": plan_id,
            "selected_server_id": server_map.get(old_server),
            "amount": amount,
            "currency": "IRR",
            "status": status,
            "order_kind": "renewal" if renewal else "purchase",
            "original_amount": amount,
            "discount_amount": 0,
            "wallet_amount": 0,
            "created_at": created,
            "updated_at": created,
            "paid_at": created if status == "fulfilled" else None,
        }
        tables["tenant_orders"].append(
            _fit_row(conn, "tenant_orders", row)
        )
        order_meta[old_id] = {
            "new_id": new_id,
            "customer_id": customer,
            "old_user_id": _int(old.get("user_id"), 0),
            "old_server_id": old_server,
            "volume": _float(old.get("volume_gb"), 0),
            "created_at": created,
            "renew_service_id": _int(old.get("renew_service_id"), 0),
            "plan_id": plan_id,
        }
    counts["orders"] = len(order_map)

    # ---------- Current subscriptions ----------
    service_map: dict[int, int] = {}
    service_order: dict[int, int] = {}
    legacy_nodes_by_service: dict[int, list[dict[str, Any]]] = {}
    for node in legacy.get("userbot_service_nodes", []):
        legacy_nodes_by_service.setdefault(
            _int(node.get("service_id"), 0), []
        ).append(node)

    def match_order(service: dict[str, Any]) -> tuple[int, int]:
        old_user = _int(service.get("user_id"), 0)
        old_server = _int(service.get("server_id"), 0)
        limit = _float(service.get("usage_limit"), 0)
        candidates = [
            (oid, meta)
            for oid, meta in order_meta.items()
            if oid not in used_order_ids_for_service
            and int(meta.get("renew_service_id") or 0) <= 0
            and meta["old_user_id"] == old_user
            and (
                meta["old_server_id"] == old_server
                or not meta["old_server_id"]
            )
        ]
        exact = [
            (oid, meta)
            for oid, meta in candidates
            if abs(float(meta["volume"]) - limit) <= 0.51
        ]
        pool = exact or candidates
        if pool:
            oid, meta = sorted(
                pool,
                key=lambda item: str(item[1]["created_at"]),
                reverse=True,
            )[0]
            used_order_ids_for_service.add(oid)
            return int(meta["new_id"]), int(meta["plan_id"])

        customer = customer_map.get(old_user)
        if not customer:
            raise LegacySellBotRestoreError("legacy service has no customer")
        plan_id = ensure_plan(
            old_server=old_server,
            title="Legacy Service",
            gb=max(1.0, limit),
            days=max(1, _int(service.get("days_left"), 30)),
            price=0,
        )
        order_id = ids.one("tenant_orders")
        tables["tenant_orders"].append(
            _fit_row(
                conn,
                "tenant_orders",
                {
                    "id": order_id,
                    "tenant_id": tid,
                    "customer_id": customer,
                    "plan_id": plan_id,
                    "selected_server_id": server_map.get(old_server),
                    "amount": 0,
                    "currency": "IRR",
                    "status": "fulfilled",
                    "order_kind": "purchase",
                    "original_amount": 0,
                    "discount_amount": 0,
                    "wallet_amount": 0,
                    "created_at": backup_time,
                    "updated_at": backup_time,
                    "paid_at": backup_time,
                },
            )
        )
        counts["orders"] += 1
        return order_id, plan_id

    for service in legacy.get("userbot_services", []):
        old_sid = _int(service.get("id"), 0)
        customer = customer_map.get(_int(service.get("user_id"), 0))
        if old_sid <= 0 or not customer:
            continue
        old_server = _int(service.get("server_id"), 0)
        order_id, plan_id = match_order(service)
        new_sid = ids.one("tenant_subscriptions")
        service_map[old_sid] = new_sid
        service_order[old_sid] = order_id
        days_left = _int(service.get("days_left"), -1)
        raw_expired = str(service.get("expired_at") or "").strip()
        if days_left >= 0:
            expires = backup_dt + timedelta(days=days_left)
            status = "active"
            expired_at = None
        else:
            parsed_expired = _iso(raw_expired, fallback=backup_time)
            try:
                expires = datetime.fromisoformat(parsed_expired.replace("Z", "+00:00"))
            except ValueError:
                expires = backup_dt - timedelta(days=1)
            status = "expired"
            expired_at = iso_utc(expires)
        node_rows = legacy_nodes_by_service.get(old_sid, [])
        primary = next(
            (
                row for row in node_rows
                if _int(row.get("server_id"), 0) == old_server
            ),
            node_rows[0] if node_rows else {},
        )
        external_ref = str(
            primary.get("panel_user_uuid")
            or primary.get("panel_user_id")
            or primary.get("marzban_username")
            or _legacy_comment_value(service.get("comment"), "uuid")
            or ""
        ).strip() or None
        row = {
            "id": new_sid,
            "tenant_id": tid,
            "customer_id": customer,
            "plan_id": plan_id,
            "order_id": order_id,
            "server_id": server_map.get(old_server),
            "external_ref": external_ref,
            "status": status,
            "usage_bytes": max(
                0, int(_float(service.get("usage_current"), 0) * 1024**3)
            ),
            "traffic_bytes": max(
                1, int(max(0.001, _float(service.get("usage_limit"), 1)) * 1024**3)
            ),
            "expires_at": iso_utc(expires),
            "last_online": _iso(
                service.get("last_online"), fallback=backup_time
            ) if str(service.get("last_online") or "").strip() else None,
            "last_synced_at": backup_time,
            "expired_at": expired_at,
            "service_name": _text(service.get("name"), 120),
            "created_at": backup_time,
            "updated_at": now,
        }
        tables["tenant_subscriptions"].append(
            _fit_row(conn, "tenant_subscriptions", row)
        )
    counts["services"] = len(service_map)

    # Renewal order links after service ids exist.
    for old_oid, meta in order_meta.items():
        old_service = int(meta.get("renew_service_id") or 0)
        if old_service <= 0 or old_service not in service_map:
            continue
        tables["tenant_renewal_orders"].append(
            _fit_row(
                conn,
                "tenant_renewal_orders",
                {
                    "order_id": int(meta["new_id"]),
                    "tenant_id": tid,
                    "subscription_id": service_map[old_service],
                    "created_at": str(meta["created_at"]),
                    "renew_volume_mode": "reset",
                    "renew_time_mode": "reset",
                },
            )
        )

    # ---------- Subscription nodes + panel inventory ----------
    # SellBot also stores AdminBot-created panel users in userbot_services with
    # user_id=0. They are not UserBot subscriptions and must not get a fake
    # customer/order. They do, however, map exactly to WhiteLabel's native
    # tenant_panel_users + tenant_panel_user_nodes inventory.
    legacy_services_by_id = {
        _int(item.get("id"), 0): item
        for item in legacy.get("userbot_services", [])
        if _int(item.get("id"), 0) > 0
    }
    admin_service_ids = {
        old_id
        for old_id, item in legacy_services_by_id.items()
        if _int(item.get("user_id"), 0) == 0
    }
    panel_user_map: dict[tuple[int, str], int] = {}
    primary_panel_user: dict[int, int] = {}
    for old_service, service in legacy_services_by_id.items():
        sub_id = service_map.get(old_service)
        is_admin_inventory = old_service in admin_service_ids
        if not sub_id and not is_admin_inventory:
            continue

        old_primary = _int(service.get("server_id"), 0)
        nodes = list(legacy_nodes_by_service.get(old_service, []))
        primary_node = next(
            (
                node
                for node in nodes
                if _int(node.get("server_id"), 0) == old_primary
            ),
            None,
        )
        comment_ref = _legacy_comment_value(service.get("comment"), "uuid")
        primary_ref = str(
            (primary_node or {}).get("panel_user_uuid")
            or (primary_node or {}).get("panel_user_id")
            or (primary_node or {}).get("marzban_username")
            or comment_ref
            or ""
        ).strip()

        # Older SellBot admin services can carry the primary UUID only inside
        # comment=uuid:...|admin:1. Synthesize only the inventory view; the raw
        # source row remains preserved byte-for-byte in the encrypted asset.
        if (
            primary_ref
            and old_primary > 0
            and not any(
                _int(node.get("server_id"), 0) == old_primary
                and str(
                    node.get("panel_user_uuid")
                    or node.get("panel_user_id")
                    or node.get("marzban_username")
                    or ""
                ).strip()
                for node in nodes
            )
        ):
            nodes.insert(
                0,
                {
                    "server_id": old_primary,
                    "panel_user_uuid": primary_ref,
                    "is_active": 1,
                    "usage_current": service.get("usage_current"),
                    "days_left": service.get("days_left"),
                    "created_at": backup_time,
                },
            )

        seen_server: set[int] = set()
        for node in nodes:
            old_server = _int(node.get("server_id"), 0)
            new_server = server_map.get(old_server)
            if not new_server or new_server in seen_server:
                continue
            seen_server.add(new_server)
            ref = str(
                node.get("panel_user_uuid")
                or node.get("panel_user_id")
                or node.get("marzban_username")
                or (primary_ref if old_server == old_primary else "")
                or ""
            ).strip()
            if not ref:
                continue
            deleted = bool(_int(node.get("deleted"), 0))
            active = bool(_int(node.get("is_active"), 1)) and not deleted
            is_primary = 1 if old_server == old_primary else 0
            status = (
                "expired"
                if not active and _int(node.get("days_left"), 0) < 0
                else "disabled"
                if not active
                else "active"
            )
            usage = max(
                0,
                int(
                    _float(
                        node.get("usage_current"),
                        _float(service.get("usage_current"), 0),
                    )
                    * 1024**3
                ),
            )

            if sub_id:
                tables["tenant_subscription_nodes"].append(
                    _fit_row(
                        conn,
                        "tenant_subscription_nodes",
                        {
                            "id": ids.one("tenant_subscription_nodes"),
                            "tenant_id": tid,
                            "subscription_id": sub_id,
                            "server_id": new_server,
                            "external_ref": ref,
                            "is_primary": is_primary,
                            "status": status,
                            "usage_bytes": usage,
                            "usage_offset_bytes": 0,
                            "last_online": (
                                _iso(service.get("last_online"), fallback=backup_time)
                                if is_primary and str(service.get("last_online") or "").strip()
                                else None
                            ),
                            "last_error": None,
                            "created_at": _iso(node.get("created_at"), fallback=backup_time),
                            "updated_at": now,
                        },
                    )
                )

            key = (new_server, ref)
            if key not in panel_user_map:
                puid = ids.one("tenant_panel_users")
                panel_user_map[key] = puid
                traffic = max(
                    1,
                    int(max(0.001, _float(service.get("usage_limit"), 1)) * 1024**3),
                )
                if sub_id:
                    expires_at = next(
                        (
                            row["expires_at"]
                            for row in tables["tenant_subscriptions"]
                            if int(row["id"]) == int(sub_id)
                        ),
                        backup_time,
                    )
                else:
                    days_left = _int(service.get("days_left"), -1)
                    expires_at = (
                        iso_utc(backup_dt + timedelta(days=days_left))
                        if days_left >= 0
                        else _iso(service.get("expired_at"), fallback=backup_time)
                    )
                tables["tenant_panel_users"].append(
                    _fit_row(
                        conn,
                        "tenant_panel_users",
                        {
                            "id": puid,
                            "tenant_id": tid,
                            "server_id": new_server,
                            "external_ref": ref,
                            "name": _text(service.get("name"), 120, ref[:32]),
                            "comment": _text(service.get("comment"), 1000),
                            "usage_bytes": usage,
                            "traffic_bytes": traffic,
                            "expires_at": expires_at,
                            "last_online": (
                                _iso(service.get("last_online"), fallback=backup_time)
                                if str(service.get("last_online") or "").strip()
                                else None
                            ),
                            "active": 1 if active else 0,
                            "state": "deleted" if deleted else "active",
                            "extra_json": json.dumps(
                                {
                                    "legacy_service_id": old_service,
                                    "legacy_admin_inventory": bool(is_admin_inventory),
                                },
                                separators=(",", ":"),
                            ),
                            "last_synced_at": now,
                        },
                    )
                )
            if is_primary:
                primary_panel_user[old_service] = panel_user_map[key]

    for old_service, service in legacy_services_by_id.items():
        source_user = primary_panel_user.get(old_service)
        if not source_user:
            continue
        old_primary = _int(service.get("server_id"), 0)
        for node in legacy_nodes_by_service.get(old_service, []):
            old_server = _int(node.get("server_id"), 0)
            if old_server == old_primary:
                continue
            new_server = server_map.get(old_server)
            ref = str(
                node.get("panel_user_uuid")
                or node.get("panel_user_id")
                or node.get("marzban_username")
                or ""
            ).strip()
            if not new_server or not ref:
                continue
            tables["tenant_panel_user_nodes"].append(
                _fit_row(
                    conn,
                    "tenant_panel_user_nodes",
                    {
                        "tenant_id": tid,
                        "source_user_id": source_user,
                        "server_id": new_server,
                        "external_ref": ref,
                        "last_error": None,
                        "fail_count": max(0, _int(node.get("fail_count"), 0)),
                        "frozen_at": str(node.get("frozen_at") or "").strip() or None,
                        "updated_at": now,
                    },
                )
            )

    admin_services_mapped = len(admin_service_ids.intersection(primary_panel_user))
    admin_services_unmapped = len(admin_service_ids) - admin_services_mapped
    counts["admin_services"] = admin_services_mapped
    counts["services"] = len(service_map) + admin_services_mapped
    if admin_services_unmapped:
        warnings.append(
            f"{admin_services_unmapped} سرویس ادمینی SellBot شناسه/سرور قابل تبدیل نداشت؛ "
            "نسخه خام آن‌ها به‌صورت رمزنگاری‌شده حفظ شد."
        )

    # ---------- Tickets ----------
    ticket_map: dict[int, int] = {}
    ticket_by_code: dict[str, int] = {}
    for ticket in legacy.get("userbot_tickets", []):
        old_id = _int(ticket.get("id"), 0)
        customer = customer_map.get(_int(ticket.get("user_id"), 0))
        if old_id <= 0 or not customer:
            continue
        new_id = ids.one("tenant_tickets")
        ticket_map[old_id] = new_id
        ticket_by_code[str(ticket.get("ticket_code") or "")] = new_id
        status_raw = str(ticket.get("status") or "open").strip().lower()
        status = status_raw if status_raw in {"open", "answered", "closed"} else "open"
        tables["tenant_tickets"].append(
            _fit_row(
                conn,
                "tenant_tickets",
                {
                    "id": new_id,
                    "tenant_id": tid,
                    "customer_id": customer,
                    "subject": _text(ticket.get("title"), 200, "Legacy ticket"),
                    "body": str(ticket.get("question") or "")[:4000],
                    "admin_reply": None,
                    "status": status,
                    "created_at": _iso(ticket.get("created_at"), fallback=backup_time),
                    "updated_at": _iso(ticket.get("updated_at"), fallback=backup_time),
                },
            )
        )
    counts["tickets"] = len(ticket_map)

    for msg in legacy.get("userbot_ticket_messages", []):
        ticket_id = ticket_by_code.get(str(msg.get("ticket_code") or ""))
        if not ticket_id:
            continue
        sender = str(msg.get("sender_type") or "user").strip().lower()
        if sender not in {"user", "admin"}:
            sender = "user"
        text_value = str(msg.get("message_text") or "").strip()
        if not text_value:
            # Telegram file_ids from the old bot are intentionally not reused
            # across bot tokens; the original row remains in legacy assets.
            text_value = "[رسانه قدیمی در فایل خام بکاپ حفظ شده است]"
        tables["tenant_ticket_messages"].append(
            _fit_row(
                conn,
                "tenant_ticket_messages",
                {
                    "id": ids.one("tenant_ticket_messages"),
                    "tenant_id": tid,
                    "ticket_id": ticket_id,
                    "sender_type": sender,
                    "sender_name": _text(msg.get("sender_name"), 120),
                    "message_text": text_value[:4000],
                    "media_mime": "",
                    "media": None,
                    "created_at": _iso(msg.get("created_at"), fallback=backup_time),
                },
            )
        )

    # ---------- Referrals ----------
    referral_map: dict[int, int] = {}
    for ref in legacy.get("userbot_referrals", []):
        inviter = customer_map.get(_int(ref.get("inviter_id"), 0))
        invitee = customer_map.get(_int(ref.get("invitee_id"), 0))
        old_ref_id = _int(ref.get("id"), 0)
        if not inviter or not invitee or old_ref_id <= 0:
            continue
        new_ref = ids.one("tenant_referrals")
        referral_map[old_ref_id] = new_ref
        status_raw = str(ref.get("status") or "active").strip().lower()
        tables["tenant_referrals"].append(
            _fit_row(
                conn,
                "tenant_referrals",
                {
                    "id": new_ref,
                    "tenant_id": tid,
                    "inviter_customer_id": inviter,
                    "invitee_customer_id": invitee,
                    "invited_by_code": _text(
                        ref.get("invited_by_code"), 80, "legacy"
                    ),
                    "qualified": 1 if ref.get("invitee_qualified", 1) else 0,
                    "fraud_flag": 1 if ref.get("fraud_flag") else 0,
                    "status": "rejected" if status_raw == "rejected" else "active",
                    "created_at": _iso(ref.get("created_at"), fallback=backup_time),
                    "updated_at": _iso(ref.get("updated_at"), fallback=backup_time),
                },
            )
        )
    for reward in legacy.get("userbot_referral_rewards", []):
        new_ref = referral_map.get(_int(reward.get("referral_id"), 0))
        inviter = customer_map.get(_int(reward.get("inviter_id"), 0))
        invitee = customer_map.get(_int(reward.get("invitee_id"), 0))
        amount = max(0, _int(reward.get("amount_toman"), 0))
        if not new_ref or not inviter or not invitee or amount <= 0:
            continue
        reward_type = str(reward.get("reward_type") or "purchase").strip().lower()
        if reward_type not in {"trial", "purchase"}:
            reward_type = "purchase"
        tables["tenant_referral_rewards"].append(
            _fit_row(
                conn,
                "tenant_referral_rewards",
                {
                    "id": ids.one("tenant_referral_rewards"),
                    "tenant_id": tid,
                    "referral_id": new_ref,
                    "inviter_customer_id": inviter,
                    "invitee_customer_id": invitee,
                    "reward_type": reward_type,
                    "amount": amount,
                    "currency": "IRR",
                    "order_id": None,
                    "status": (
                        "revoked"
                        if str(reward.get("status") or "").lower() == "revoked"
                        else "paid"
                    ),
                    "created_at": _iso(reward.get("created_at"), fallback=backup_time),
                },
            )
        )

    # ---------- Traffic baseline ----------
    for row in legacy.get("server_traffic_daily", []):
        server = server_map.get(_int(row.get("server_id"), 0))
        day = str(row.get("day") or "").strip()
        if not server or not day:
            continue
        tables["tenant_server_traffic_daily"].append(
            _fit_row(
                conn,
                "tenant_server_traffic_daily",
                {
                    "tenant_id": tid,
                    "server_id": server,
                    "day": day,
                    "baseline_gb": max(0.0, _float(row.get("baseline_gb"), 0)),
                    "last_total_gb": max(0.0, _float(row.get("last_total_gb"), 0)),
                    "updated_at": _iso(row.get("updated_at"), fallback=backup_time),
                },
            )
        )

    # ---------- Lossless source preservation ----------
    asset_rows: list[dict[str, Any]] = []
    for path, raw in preflight["members"].items():
        asset_rows.append(
            _fit_row(
                conn,
                "tenant_legacy_restore_assets",
                {
                    "id": ids.one("tenant_legacy_restore_assets"),
                    "tenant_id": tid,
                    "restore_id": restore_id,
                    "path": path,
                    "kind": _legacy_asset_kind(path),
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "encoding": "fernet-chunked-b64",
                    "content": _blob_marker(_encrypt_legacy_asset(cipher, raw)),
                    "created_at": now,
                },
            )
        )
    tables["tenant_legacy_restore_assets"] = asset_rows
    counts["assets"] = len(asset_rows)

    summary = {
        **counts,
        "source_members": len(preflight["index"]),
        "warnings": list(warnings),
        "agent_customer_data_preserved": any(
            path in preflight["members"]
            for path in (
                "Shared/agency.db",
                "customer_bot.db",
                "AgentBot/agent_bot.db",
            )
        ),
        "panel_backups_applied": False,
    }
    tables["tenant_legacy_restore_runs"] = [
        _fit_row(
            conn,
            "tenant_legacy_restore_runs",
            {
                "id": ids.one("tenant_legacy_restore_runs"),
                "tenant_id": tid,
                "restore_id": restore_id,
                "source_format": LEGACY_FORMAT,
                "source_sha256": source_sha,
                "status": "completed",
                "summary_json": json.dumps(
                    summary, ensure_ascii=False, separators=(",", ":")
                ),
                "created_at": now,
            },
        )
    ]

    snapshot = {
        "format": BACKUP_FORMAT,
        "tenant_id": tid,
        "created_at": now,
        "tenant": {"id": tid},
        "schema_versions": [],
        "excluded_tables": ["tenant_bots", "tenant_backup_runs"],
        "tables": tables,
    }
    return snapshot, counts, warnings, restore_id


def restore_sellbot_backup(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    owner_telegram_id: int,
    cipher: TokenCipher,
    data: bytes,
) -> LegacySellBotRestoreReport:
    """Preflight and atomically import one real SellBot Backup_All archive."""
    if cipher is None:
        raise LegacySellBotRestoreError(
            "Tenant secret encryption is required for SellBot restore"
        )
    preflight = _preflight_sellbot_backup(bytes(data))
    snapshot, counts, warnings, _restore_id = _build_snapshot(
        conn,
        tenant_id=int(tenant_id),
        owner_telegram_id=int(owner_telegram_id),
        cipher=cipher,
        data=bytes(data),
        preflight=preflight,
    )
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    native = restore_tenant_backup(
        conn,
        tenant_id=int(tenant_id),
        data=encoded,
    )
    return LegacySellBotRestoreReport(
        tenant_id=int(tenant_id),
        format=LEGACY_FORMAT,
        tables_restored=int(native.tables_restored),
        rows_restored=int(native.rows_restored),
        table_names=tuple(native.table_names),
        source_members=len(preflight["index"]),
        assets_preserved=int(counts["assets"]),
        users=int(counts["users"]),
        orders=int(counts["orders"]),
        services=int(counts["services"]),
        tickets=int(counts["tickets"]),
        panel_backups=int(counts["panel_backups"]),
        warnings=tuple(warnings),
    )
