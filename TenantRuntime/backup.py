"""Tenant-scoped backup/restore and durable auto-backup primitives.

Phase 15 intentionally backs up *all* tenant-scoped tables discovered from the
live schema, instead of maintaining a fragile hand-written list.  Runtime bot
credentials and backup scheduler bookkeeping are excluded on purpose: a Tenant
restore must not silently rotate its currently running AdminBot/UserBot tokens
or replay old auto-backup slots.

Format v2 is a ZIP with:
- manifest.json: format/version, tenant id, SHA-256 and table counts
- tenant.json: JSON payload with binary SQLite values encoded as base64 markers

Legacy Phase-14 JSON backups (hiddify-whitelabel-tenant-userbot-v1) remain
accepted for backward compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from io import BytesIO
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from Database.connection import transaction
from Database.repositories import BotRepository
from Shared.crypto import TokenCipher, fingerprint_token
from Shared.timeutils import iso_utc, parse_utc, utcnow


BACKUP_FORMAT = "hiddify-whitelabel-tenant-v2"
LEGACY_FORMAT = "hiddify-whitelabel-tenant-userbot-v1"
MAX_BACKUP_BYTES = 128 * 1024 * 1024
MAX_ZIP_MEMBERS = 8
AUTO_BACKUP_TIMEZONE = "Asia/Tehran"
AUTO_BACKUP_HOURS = (0, 6, 12, 18)
AUTO_BACKUP_MAX_ATTEMPTS = 3
AUTO_BACKUP_STALE_MINUTES = 10

# tenant_bots contains the live AdminBot/UserBot credentials managed by
# MasterBot.  Restoring it from a Tenant backup could invalidate the bot that
# is performing the restore.  tenant_backup_runs is scheduler bookkeeping,
# never business state.
BACKUP_EXCLUDED_TABLES = frozenset({
    "tenant_bots",
    "tenant_backup_runs",
})


class TenantBackupError(RuntimeError):
    """Backup/restore validation or application failed."""


@dataclass(frozen=True)
class TenantBackupArtifact:
    tenant_id: int
    filename: str
    data: bytes
    sha256: str
    table_count: int
    row_count: int


@dataclass(frozen=True)
class TenantRestoreReport:
    tenant_id: int
    format: str
    tables_restored: int
    rows_restored: int
    table_names: tuple[str, ...]


@dataclass(frozen=True)
class AutoBackupDelivery:
    tenant_id: int
    owner_telegram_id: int
    slot_key: str
    bot_token: str
    event_target: str
    artifact: TenantBackupArtifact


class AutoBackupSender:
    async def send(self, delivery: AutoBackupDelivery) -> None:
        raise NotImplementedError


class TelegramAutoBackupSender(AutoBackupSender):
    """Deliver auto backups with the Tenant's own AdminBot credential."""

    async def send(self, delivery: AutoBackupDelivery) -> None:
        from telegram import Bot

        caption = (
            "⏰ بکاپ خودکار کامل Tenant\n"
            f"🕐 بازه: {delivery.slot_key}\n"
            f"🗂 جداول: {delivery.artifact.table_count}\n"
            f"📊 رکوردها: {delivery.artifact.row_count}"
        )
        async with Bot(token=delivery.bot_token) as bot:
            primary = BytesIO(delivery.artifact.data)
            primary.name = delivery.artifact.filename
            await bot.send_document(
                chat_id=int(delivery.owner_telegram_id),
                document=primary,
                filename=delivery.artifact.filename,
                caption=caption,
            )
            target = str(delivery.event_target or "").strip()
            if target and target != str(delivery.owner_telegram_id):
                try:
                    event_file = BytesIO(delivery.artifact.data)
                    event_file.name = delivery.artifact.filename
                    chat_target: Any = (
                        int(target) if target.lstrip("-").isdigit() else target
                    )
                    await bot.send_document(
                        chat_id=chat_target,
                        document=event_file,
                        filename=delivery.artifact.filename,
                        caption=caption,
                    )
                except Exception:
                    # Event-channel delivery is secondary. Owner delivery is the
                    # durable success criterion, matching SellBot behavior.
                    pass


def _qid(name: str) -> str:
    clean = str(name or "")
    if not clean or "\x00" in clean:
        raise TenantBackupError("invalid sqlite identifier")
    return '"' + clean.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA table_info({_qid(table)})").fetchall())


def _tenant_tables(conn: sqlite3.Connection) -> list[str]:
    """Return every live tenant-scoped table that owns a tenant_id column."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name LIKE 'tenant_%' ORDER BY name"
    ).fetchall()
    result: list[str] = []
    for row in rows:
        name = str(row["name"])
        if name in BACKUP_EXCLUDED_TABLES:
            continue
        columns = _table_columns(conn, name)
        if any(str(col["name"]) == "tenant_id" for col in columns):
            result.append(name)
    return result


def tenant_backup_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Public read-only schema contract used by tests and future extensions."""
    return tuple(_tenant_tables(conn))


def _encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"__wl_bytes_b64__": base64.b64encode(raw).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if (
        isinstance(value, dict)
        and set(value) == {"__wl_bytes_b64__"}
        and isinstance(value.get("__wl_bytes_b64__"), str)
    ):
        try:
            return base64.b64decode(
                value["__wl_bytes_b64__"].encode("ascii"),
                validate=True,
            )
        except Exception as exc:
            raise TenantBackupError("invalid base64 value in backup") from exc
    return value


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = _table_columns(conn, table)
    ranked = sorted(
        (
            (int(col["pk"]), str(col["name"]))
            for col in cols
            if int(col["pk"] or 0) > 0
        ),
        key=lambda item: item[0],
    )
    return [name for _rank, name in ranked]


def _snapshot_rows(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    table: str,
) -> list[dict[str, Any]]:
    pk = _pk_columns(conn, table)
    order_sql = ""
    if pk:
        order_sql = " ORDER BY " + ",".join(_qid(name) for name in pk)
    rows = conn.execute(
        f"SELECT * FROM {_qid(table)} WHERE tenant_id=?" + order_sql,
        (int(tenant_id),),
    ).fetchall()
    return [
        {str(key): _encode_value(row[key]) for key in row.keys()}
        for row in rows
    ]


def build_tenant_snapshot(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
) -> dict[str, Any]:
    tenant = conn.execute(
        "SELECT id, public_id, owner_telegram_id, status, created_at, updated_at "
        "FROM tenants WHERE id=?",
        (int(tenant_id),),
    ).fetchone()
    if tenant is None:
        raise TenantBackupError("tenant not found")

    tables: dict[str, list[dict[str, Any]]] = {}
    for table in _tenant_tables(conn):
        tables[table] = _snapshot_rows(
            conn,
            tenant_id=int(tenant_id),
            table=table,
        )

    migrations = [
        str(row["version"])
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    return {
        "format": BACKUP_FORMAT,
        "tenant_id": int(tenant_id),
        "created_at": iso_utc(utcnow()),
        "tenant": dict(tenant),
        "schema_versions": migrations,
        "excluded_tables": sorted(BACKUP_EXCLUDED_TABLES),
        "tables": tables,
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_tenant_backup(snapshot: dict[str, Any]) -> TenantBackupArtifact:
    if str(snapshot.get("format") or "") != BACKUP_FORMAT:
        raise TenantBackupError("invalid snapshot format")
    tenant_id = int(snapshot.get("tenant_id") or 0)
    if tenant_id <= 0:
        raise TenantBackupError("invalid snapshot tenant")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise TenantBackupError("invalid snapshot tables")

    payload = _json_bytes(snapshot)
    payload_sha = hashlib.sha256(payload).hexdigest()
    row_count = sum(
        len(rows)
        for rows in tables.values()
        if isinstance(rows, list)
    )
    manifest = {
        "format": BACKUP_FORMAT,
        "tenant_id": tenant_id,
        "created_at": str(snapshot.get("created_at") or ""),
        "payload": "tenant.json",
        "payload_sha256": payload_sha,
        "table_count": len(tables),
        "row_count": row_count,
        "tables": {
            str(name): len(rows) if isinstance(rows, list) else -1
            for name, rows in sorted(tables.items())
        },
    }

    bio = io.BytesIO()
    with zipfile.ZipFile(
        bio,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        zf.writestr("tenant.json", payload)
        zf.writestr("manifest.json", _json_bytes(manifest))
    data = bio.getvalue()
    if len(data) > MAX_BACKUP_BYTES:
        raise TenantBackupError("tenant backup exceeds size limit")
    created = str(snapshot.get("created_at") or "").replace(":", "-")
    created = created.replace("+", "_").replace("T", "_")
    filename = f"Tenant_{tenant_id}_Backup_{created}.zip"
    return TenantBackupArtifact(
        tenant_id=tenant_id,
        filename=filename,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        table_count=len(tables),
        row_count=row_count,
    )


def create_tenant_backup(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
) -> TenantBackupArtifact:
    return encode_tenant_backup(
        build_tenant_snapshot(conn, tenant_id=int(tenant_id))
    )


def _decode_zip(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BACKUP_BYTES:
        raise TenantBackupError("backup exceeds size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as zf:
            infos = zf.infolist()
            if not infos or len(infos) > MAX_ZIP_MEMBERS:
                raise TenantBackupError("invalid backup zip member count")
            names = {str(info.filename) for info in infos if not info.is_dir()}
            if names != {"manifest.json", "tenant.json"}:
                raise TenantBackupError("invalid backup zip layout")
            total = sum(int(info.file_size or 0) for info in infos)
            if total > MAX_BACKUP_BYTES:
                raise TenantBackupError("backup expands beyond size limit")
            bad = zf.testzip()
            if bad:
                raise TenantBackupError("backup zip CRC check failed")
            manifest_raw = zf.read("manifest.json")
            payload = zf.read("tenant.json")
    except TenantBackupError:
        raise
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise TenantBackupError("invalid backup zip") from exc

    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        snapshot = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TenantBackupError("backup JSON is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(snapshot, dict):
        raise TenantBackupError("backup payload is invalid")
    if str(manifest.get("format") or "") != BACKUP_FORMAT:
        raise TenantBackupError("unsupported backup format")
    if str(snapshot.get("format") or "") != BACKUP_FORMAT:
        raise TenantBackupError("snapshot format mismatch")
    expected = str(manifest.get("payload_sha256") or "").lower()
    actual = hashlib.sha256(payload).hexdigest()
    if expected != actual:
        raise TenantBackupError("backup payload checksum mismatch")
    if int(manifest.get("tenant_id") or 0) != int(snapshot.get("tenant_id") or 0):
        raise TenantBackupError("backup tenant mismatch")
    return snapshot


def _decode_json(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BACKUP_BYTES:
        raise TenantBackupError("backup exceeds size limit")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TenantBackupError("backup JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise TenantBackupError("backup JSON must be an object")
    return payload


def decode_tenant_backup(data: bytes) -> dict[str, Any]:
    raw = bytes(data)
    if raw.startswith(b"PK\x03\x04"):
        return _decode_zip(raw)
    return _decode_json(raw)


def _foreign_dependencies(
    conn: sqlite3.Connection,
    table: str,
    included: set[str],
) -> set[str]:
    deps: set[str] = set()
    for row in conn.execute(
        f"PRAGMA foreign_key_list({_qid(table)})"
    ).fetchall():
        parent = str(row["table"])
        if parent in included and parent != table:
            deps.add(parent)
    return deps


def _topological_tables(
    conn: sqlite3.Connection,
    tables: set[str],
) -> list[str]:
    remaining = {
        table: _foreign_dependencies(conn, table, tables)
        for table in tables
    }
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            table
            for table, deps in remaining.items()
            if not (deps & set(remaining))
        )
        if not ready:
            # Defensive fallback for an unexpected FK cycle. SQLite can defer
            # neither every legacy FK nor every UNIQUE constraint reliably;
            # fail rather than partially applying a cyclic restore.
            raise TenantBackupError("tenant backup tables contain an FK cycle")
        for table in ready:
            ordered.append(table)
            remaining.pop(table, None)
    return ordered


def _normalize_snapshot_for_restore(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    tenant_id: int,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    fmt = str(payload.get("format") or "")
    if fmt not in {BACKUP_FORMAT, LEGACY_FORMAT}:
        raise TenantBackupError("unsupported tenant backup format")
    if int(payload.get("tenant_id") or 0) != int(tenant_id):
        raise TenantBackupError("backup belongs to another tenant")
    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, dict) or not raw_tables:
        raise TenantBackupError("backup has no tenant tables")

    live = set(_tenant_tables(conn))
    tables: dict[str, list[dict[str, Any]]] = {}
    for raw_name, raw_rows in raw_tables.items():
        table = str(raw_name)
        if table not in live:
            raise TenantBackupError(f"backup table is not supported: {table}")
        if not isinstance(raw_rows, list):
            raise TenantBackupError(f"invalid rows for table: {table}")
        allowed_columns = {
            str(row["name"])
            for row in _table_columns(conn, table)
        }
        prepared: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise TenantBackupError(f"invalid row in table: {table}")
            if set(map(str, raw_row.keys())) - allowed_columns:
                raise TenantBackupError(f"unknown column in backup table: {table}")
            decoded = {
                str(key): _decode_value(value)
                for key, value in raw_row.items()
            }
            if int(decoded.get("tenant_id") or 0) != int(tenant_id):
                raise TenantBackupError("cross-tenant row in backup")
            prepared.append(decoded)
        tables[table] = prepared
    return fmt, tables


def _preflight_primary_key_collisions(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    tables: dict[str, list[dict[str, Any]]],
) -> None:
    for table, rows in tables.items():
        pk = _pk_columns(conn, table)
        if not pk:
            continue
        for row in rows:
            if any(column not in row for column in pk):
                raise TenantBackupError(
                    f"backup row is missing primary key columns: {table}"
                )
            where = " AND ".join(f"{_qid(column)}=?" for column in pk)
            values = tuple(row[column] for column in pk)
            existing = conn.execute(
                f"SELECT tenant_id FROM {_qid(table)} WHERE {where} LIMIT 1",
                values,
            ).fetchone()
            if (
                existing is not None
                and int(existing["tenant_id"]) != int(tenant_id)
            ):
                raise TenantBackupError(
                    f"backup primary key belongs to another tenant: {table}"
                )


def restore_tenant_backup(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    data: bytes,
) -> TenantRestoreReport:
    payload = decode_tenant_backup(data)
    fmt, tables = _normalize_snapshot_for_restore(
        conn,
        payload,
        tenant_id=int(tenant_id),
    )
    _preflight_primary_key_collisions(
        conn,
        tenant_id=int(tenant_id),
        tables=tables,
    )
    included = set(tables)
    ordered = _topological_tables(conn, included)
    restored = 0

    try:
        with transaction(conn):
            if fmt == BACKUP_FORMAT:
                # Full v2 restore: child rows first during deletion; parent rows
                # first on insert.  The entire mutation is one transaction.
                for table in reversed(ordered):
                    conn.execute(
                        f"DELETE FROM {_qid(table)} WHERE tenant_id=?",
                        (int(tenant_id),),
                    )

            for table in ordered:
                columns_live = {
                    str(col["name"]): col
                    for col in _table_columns(conn, table)
                }
                pk = _pk_columns(conn, table)
                for row in tables[table]:
                    if not row:
                        raise TenantBackupError(f"empty row in table: {table}")
                    columns = list(row.keys())
                    # tenant_id is mandatory for every backed-up table.
                    if "tenant_id" not in columns or "tenant_id" not in columns_live:
                        raise TenantBackupError(f"invalid tenant table row: {table}")
                    names = ",".join(_qid(name) for name in columns)
                    marks = ",".join("?" for _ in columns)
                    values = tuple(row[name] for name in columns)
                    if fmt == LEGACY_FORMAT and pk:
                        # Phase-14 backups were config-only and intentionally
                        # non-destructive. Preserve that contract: update same
                        # primary keys but never delete operational history.
                        update_cols = [
                            name for name in columns if name not in set(pk)
                        ]
                        if update_cols:
                            conflict = ",".join(_qid(name) for name in pk)
                            updates = ",".join(
                                f"{_qid(name)}=excluded.{_qid(name)}"
                                for name in update_cols
                            )
                            conn.execute(
                                f"INSERT INTO {_qid(table)} ({names}) VALUES ({marks}) "
                                f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}",
                                values,
                            )
                        else:
                            conn.execute(
                                f"INSERT OR IGNORE INTO {_qid(table)} ({names}) "
                                f"VALUES ({marks})",
                                values,
                            )
                    else:
                        conn.execute(
                            f"INSERT INTO {_qid(table)} ({names}) VALUES ({marks})",
                            values,
                        )
                    restored += 1
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise TenantBackupError("restored tenant data violates foreign keys")
    except TenantBackupError:
        raise
    except sqlite3.DatabaseError as exc:
        raise TenantBackupError("tenant restore transaction failed") from exc

    return TenantRestoreReport(
        tenant_id=int(tenant_id),
        format=fmt,
        tables_restored=len(tables),
        rows_restored=restored,
        table_names=tuple(ordered),
    )


def auto_backup_slot_key(
    now: datetime | None = None,
    *,
    timezone_name: str = AUTO_BACKUP_TIMEZONE,
) -> str:
    tz = ZoneInfo(str(timezone_name))
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("UTC"))
    local = current.astimezone(tz)
    hour = max(h for h in AUTO_BACKUP_HOURS if h <= local.hour)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot.isoformat(timespec="minutes")


def claim_auto_backup_slot(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    slot_key: str,
    max_attempts: int = AUTO_BACKUP_MAX_ATTEMPTS,
) -> bool:
    key = str(slot_key or "").strip()
    if not key:
        raise ValueError("backup slot key is required")
    now = utcnow()
    now_text = iso_utc(now)
    attempts_limit = max(1, int(max_attempts))
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM tenant_backup_runs WHERE tenant_id=? AND slot_key=?",
            (int(tenant_id), key),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tenant_backup_runs "
                "(tenant_id,slot_key,status,attempts,started_at) "
                "VALUES (?,?,'claimed',1,?)",
                (int(tenant_id), key, now_text),
            )
            return True
        status = str(row["status"] or "")
        attempts = int(row["attempts"] or 0)
        if status == "success" or attempts >= attempts_limit:
            return False
        reclaim = status == "failed"
        if status == "claimed":
            try:
                started = parse_utc(str(row["started_at"]))
                reclaim = started <= now - timedelta(
                    minutes=AUTO_BACKUP_STALE_MINUTES
                )
            except Exception:
                reclaim = True
        if not reclaim:
            return False
        conn.execute(
            "UPDATE tenant_backup_runs SET status='claimed',attempts=?,"
            "started_at=?,completed_at=NULL,last_error='' "
            "WHERE tenant_id=? AND slot_key=?",
            (attempts + 1, now_text, int(tenant_id), key),
        )
        return True


def finish_auto_backup_slot(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    slot_key: str,
    success: bool,
    file_size: int = 0,
    sha256: str = "",
    error: str = "",
) -> None:
    with transaction(conn):
        changed = conn.execute(
            "UPDATE tenant_backup_runs SET status=?,completed_at=?,file_size=?,"
            "sha256=?,last_error=? WHERE tenant_id=? AND slot_key=? "
            "AND status='claimed'",
            (
                "success" if bool(success) else "failed",
                iso_utc(utcnow()),
                max(0, int(file_size)),
                str(sha256 or "")[:64],
                str(error or "")[:300],
                int(tenant_id),
                str(slot_key),
            ),
        )
        if changed.rowcount != 1:
            raise TenantBackupError("auto-backup slot is not claimed")


def _active_admin_bot_token(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    cipher: TokenCipher,
) -> str | None:
    row = BotRepository(conn).get_by_tenant_role(int(tenant_id), "admin")
    if row is None or str(row.get("status") or "") != "active":
        # A partially provisioned Tenant is not an auto-backup failure.  Do
        # not consume retry attempts before its AdminBot exists.
        return None
    token = cipher.decrypt(str(row["encrypted_token"]))
    if fingerprint_token(token) != str(row["token_fingerprint"]):
        raise TenantBackupError("tenant AdminBot credential integrity failed")
    return token


def prepare_auto_backup_delivery(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    owner_telegram_id: int,
    cipher: TokenCipher,
    settings: dict[str, Any],
    now: datetime | None = None,
) -> AutoBackupDelivery | None:
    """Claim one six-hour slot and build the exact artifact once.

    Returns None when disabled/already completed/currently claimed. Creation or
    credential errors mark the slot failed so a later lifecycle pass may retry.
    """
    if not bool(settings.get("auto_backup_enabled", True)):
        return None
    token = _active_admin_bot_token(
        conn,
        tenant_id=int(tenant_id),
        cipher=cipher,
    )
    if not token:
        return None
    slot = auto_backup_slot_key(now)
    if not claim_auto_backup_slot(
        conn,
        tenant_id=int(tenant_id),
        slot_key=slot,
    ):
        return None
    try:
        artifact = create_tenant_backup(
            conn,
            tenant_id=int(tenant_id),
        )
        target = ""
        if bool(settings.get("system_event_channel_enabled", False)):
            target = str(settings.get("system_event_channel_id") or "").strip()
        return AutoBackupDelivery(
            tenant_id=int(tenant_id),
            owner_telegram_id=int(owner_telegram_id),
            slot_key=slot,
            bot_token=token,
            event_target=target,
            artifact=artifact,
        )
    except Exception as exc:
        try:
            finish_auto_backup_slot(
                conn,
                tenant_id=int(tenant_id),
                slot_key=slot,
                success=False,
                error=type(exc).__name__,
            )
        except Exception:
            pass
        raise


def complete_auto_backup_delivery(
    conn: sqlite3.Connection,
    *,
    delivery: AutoBackupDelivery,
    success: bool,
    error: str = "",
) -> None:
    finish_auto_backup_slot(
        conn,
        tenant_id=int(delivery.tenant_id),
        slot_key=str(delivery.slot_key),
        success=bool(success),
        file_size=len(delivery.artifact.data) if success else 0,
        sha256=delivery.artifact.sha256 if success else "",
        error=str(error or ""),
    )
    if success:
        prune_backup_runs(
            conn,
            tenant_id=int(delivery.tenant_id),
            keep=64,
        )


def prune_backup_runs(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    keep: int = 64,
) -> int:
    limit = max(8, int(keep))
    rows = conn.execute(
        "SELECT slot_key FROM tenant_backup_runs WHERE tenant_id=? "
        "ORDER BY slot_key DESC",
        (int(tenant_id),),
    ).fetchall()
    stale = [str(row["slot_key"]) for row in rows[limit:]]
    if not stale:
        return 0
    with transaction(conn):
        for slot in stale:
            conn.execute(
                "DELETE FROM tenant_backup_runs WHERE tenant_id=? AND slot_key=?",
                (int(tenant_id), slot),
            )
    return len(stale)
