"""Repository layer: parameterized SQL, tenant-scoped reads, validated writes."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

from Shared.crypto import (
    TokenCipher,
    build_bot_credential,
    generate_public_id,
    validate_encrypted_token,
    validate_fingerprint,
    validate_hash,
    validate_tail,
)
from Shared.redaction import redact_text
from Shared.timeutils import iso_utc, parse_utc, utcnow

SLUG_RE = re.compile(r"^[a-z0-9-]{3,64}$")

# Key fragments that must never appear in audit metadata key names, at any depth.
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "passwd",
    "password",
    "apikey",
    "encryptionkey",
    "privatekey",
)

CURRENT_LICENSE_STATUSES = frozenset({"active", "grace"})


class TenantMismatchError(ValueError):
    """An operation named a tenant that does not own the target row. No write happened."""


class LicenseConflictError(ValueError):
    """A second current (active/grace) license was requested for one tenant."""


class InvalidBotTokenFieldError(ValueError):
    """A bot token field (encrypted_token / fingerprint / tail / hash) fails format validation."""


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key or "").strip().lower())
    if not normalized:
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


_JSON_SAFE_TYPES = (type(None), bool, int, float, str, list, tuple, dict)


def _redact_values(value: Any) -> Any:
    """Recursively redact secrets from all string values; reject non-JSON types."""
    if isinstance(value, dict):
        return {k: _redact_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_values(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if type(value) not in _JSON_SAFE_TYPES:
        raise ValueError("audit metadata must contain only JSON-safe types")
    return value


def _check_sensitive_keys(value: Any) -> None:
    """Recursively reject any key name containing a sensitive fragment (at any depth)."""
    if isinstance(value, dict):
        for key, val in value.items():
            if _is_sensitive_key(key):
                raise ValueError("audit metadata must not contain sensitive data")
            _check_sensitive_keys(val)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_sensitive_keys(item)


def validate_slug(slug: str) -> str:
    text = str(slug or "").strip().lower()
    if not SLUG_RE.match(text):
        raise ValueError("invalid slug (use 3-64 chars: a-z 0-9 -)")
    return text


def sanitize_metadata(metadata: Optional[dict[str, Any]]) -> str:
    """Validate audit metadata; redact secret content from values; reject secret key names.

    - Every string value leaf is passed through ``redact_text`` before storage.
    - A key name containing a sensitive fragment at ANY depth (token/secret/password etc.)
      raises ``ValueError`` unconditionally — the naming itself is a design flaw.
    """
    data = dict(metadata or {})
    _check_sensitive_keys(data)
    safe = _redact_values(data)
    try:
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError("audit metadata is not JSON-serializable") from exc


def canonical_license_range(
    starts_at: Any, expires_at: Any, grace_until: Any = None
) -> tuple[str, str, Optional[str]]:
    try:
        starts = parse_utc(str(starts_at or ""))
    except Exception as exc:
        raise ValueError("invalid starts_at timestamp") from exc
    try:
        expires = parse_utc(str(expires_at or ""))
    except Exception as exc:
        raise ValueError("invalid expires_at timestamp") from exc
    grace = None
    if grace_until is not None and str(grace_until).strip():
        try:
            grace = parse_utc(str(grace_until))
        except Exception as exc:
            raise ValueError("invalid grace_until timestamp") from exc
    if not expires > starts:
        raise ValueError("expires_at must be after starts_at")
    if grace is not None and grace < expires:
        raise ValueError("grace_until must not be before expires_at")
    return iso_utc(starts), iso_utc(expires), (iso_utc(grace) if grace else None)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


class TenantRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
        status: str = "active",
    ) -> dict[str, Any]:
        slug = validate_slug(slug)
        if not str(name or "").strip():
            raise ValueError("name is required")
        if int(owner_telegram_id) <= 0:
            raise ValueError("owner_telegram_id must be positive")
        if status not in ("active", "suspended", "disabled"):
            raise ValueError("invalid tenant status")
        now = iso_utc(utcnow())
        public_id = generate_public_id()
        cursor = self.conn.execute(
            "INSERT INTO tenants (public_id, name, slug, owner_telegram_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (public_id, name.strip(), slug, int(owner_telegram_id), status, now, now),
        )
        row = self.get_by_id(int(cursor.lastrowid or 0))
        assert row is not None
        return row

    def get_by_id(self, tenant_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM tenants WHERE id = ?", (int(tenant_id),)).fetchone()
        return _row_to_dict(row)

    def get_by_public_id(self, public_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenants WHERE public_id = ?", (str(public_id or "").strip(),)
        ).fetchone()
        return _row_to_dict(row)

    def get_by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenants WHERE slug = ?", (str(slug or "").strip().lower(),)
        ).fetchone()
        return _row_to_dict(row)

    def list(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        rows = self.conn.execute(
            "SELECT * FROM tenants ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            return []
        like = f"%{text}%"
        if text.lstrip("-").isdigit():
            rows = self.conn.execute(
                "SELECT * FROM tenants WHERE owner_telegram_id = ? OR name LIKE ?"
                " OR slug LIKE ? OR public_id LIKE ? ORDER BY id ASC LIMIT ? OFFSET ?",
                (
                    int(text), like, like, like,
                    max(1, min(int(limit), 100)), max(0, int(offset)),
                ),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tenants WHERE name LIKE ? OR slug LIKE ? OR public_id LIKE ?"
                " ORDER BY id ASC LIMIT ? OFFSET ?",
                (like, like, like, max(1, min(int(limit), 100)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, tenant_id: int, status: str) -> Optional[dict[str, Any]]:
        if status not in ("active", "suspended", "disabled"):
            raise ValueError("invalid tenant status")
        self.conn.execute(
            "UPDATE tenants SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso_utc(utcnow()), int(tenant_id)),
        )
        return self.get_by_id(int(tenant_id))

    def update_details(
        self,
        tenant_id: int,
        *,
        name: str,
        slug: str,
        owner_telegram_id: int,
    ) -> Optional[dict[str, Any]]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        clean_slug = validate_slug(slug)
        if int(owner_telegram_id) <= 0:
            raise ValueError("owner_telegram_id must be positive")
        self.conn.execute(
            "UPDATE tenants SET name = ?, slug = ?, owner_telegram_id = ?, updated_at = ?"
            " WHERE id = ?",
            (
                clean_name,
                clean_slug,
                int(owner_telegram_id),
                iso_utc(utcnow()),
                int(tenant_id),
            ),
        )
        return self.get_by_id(int(tenant_id))

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS total FROM tenants").fetchone()
        return int(row["total"] if row else 0)


class TenantRuntimeRepository:
    """One tenant-scoped namespace and lifecycle row per provisioned tenant."""

    STATUSES = frozenset({"provisioning", "ready", "disabled", "error"})

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        tenant_id: int,
        status: str = "provisioning",
    ) -> dict[str, Any]:
        if status not in self.STATUSES:
            raise ValueError("invalid runtime status")
        namespace = generate_public_id(18)
        now = iso_utc(utcnow())
        cursor = self.conn.execute(
            "INSERT INTO tenant_runtime_configs"
            " (tenant_id, data_namespace, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (int(tenant_id), namespace, status, now, now),
        )
        row = self.get_by_tenant(int(tenant_id))
        assert row is not None and int(cursor.rowcount) == 1
        return row

    def get_by_tenant(self, tenant_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenant_runtime_configs WHERE tenant_id = ?",
            (int(tenant_id),),
        ).fetchone()
        return _row_to_dict(row)

    def get_by_namespace(self, data_namespace: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenant_runtime_configs WHERE data_namespace = ?",
            (str(data_namespace or "").strip(),),
        ).fetchone()
        return _row_to_dict(row)

    def update_status(self, tenant_id: int, status: str) -> Optional[dict[str, Any]]:
        if status not in self.STATUSES:
            raise ValueError("invalid runtime status")
        self.conn.execute(
            "UPDATE tenant_runtime_configs SET status = ?, updated_at = ?"
            " WHERE tenant_id = ?",
            (status, iso_utc(utcnow()), int(tenant_id)),
        )
        return self.get_by_tenant(int(tenant_id))


class BotRepository:
    """Tenant bots: at most ONE row per (tenant, role); readiness = both active."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def register(
        self,
        *,
        tenant_id: int,
        role: str,
        cipher: TokenCipher,
        plain_token: str,
        telegram_bot_id: Optional[int] = None,
        telegram_username: Optional[str] = None,
        webhook_secret_hash: Optional[str] = None,
    ) -> dict[str, Any]:
        if role not in ("admin", "user"):
            raise ValueError("role must be admin or user")
        try:
            credential = build_bot_credential(cipher, plain_token)
            encrypted_token = credential["encrypted_token"]
            token_fingerprint = credential["token_fingerprint"]
            token_tail = credential["token_tail"]
            encrypted_token = validate_encrypted_token(encrypted_token)
            token_fingerprint = validate_fingerprint(token_fingerprint)
            token_tail = validate_tail(token_tail)
            webhook_secret_hash = validate_hash(webhook_secret_hash)
            if cipher.decrypt(encrypted_token) != plain_token:
                raise ValueError("cipher round-trip mismatch")
        except Exception:
            raise InvalidBotTokenFieldError("invalid bot credential") from None
        if telegram_bot_id is not None and int(telegram_bot_id) <= 0:
            raise ValueError("telegram_bot_id must be positive")
        now = iso_utc(utcnow())
        cursor = self.conn.execute(
            "INSERT INTO tenant_bots (tenant_id, role, encrypted_token, token_fingerprint, token_tail,"
            " telegram_bot_id, telegram_username, status, webhook_secret_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                int(tenant_id), role, encrypted_token, token_fingerprint, token_tail,
                int(telegram_bot_id) if telegram_bot_id is not None else None,
                (telegram_username or None), webhook_secret_hash, now, now,
            ),
        )
        row = self.get_by_id(int(cursor.lastrowid or 0))
        assert row is not None
        return row

    def get_by_id(self, bot_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM tenant_bots WHERE id = ?", (int(bot_id),)).fetchone()
        return _row_to_dict(row)

    def get_by_fingerprint(self, fingerprint: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenant_bots WHERE token_fingerprint = ?",
            (str(fingerprint or "").strip(),),
        ).fetchone()
        return _row_to_dict(row)

    def get_by_telegram_id(self, telegram_bot_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenant_bots WHERE telegram_bot_id = ?", (int(telegram_bot_id),)
        ).fetchone()
        return _row_to_dict(row)

    def get_by_tenant_role(self, tenant_id: int, role: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM tenant_bots WHERE tenant_id = ? AND role = ?",
            (int(tenant_id), role),
        ).fetchone()
        return _row_to_dict(row)

    def list_by_tenant(self, tenant_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tenant_bots WHERE tenant_id = ? ORDER BY role ASC", (int(tenant_id),)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_runtime_candidates(
        self, *, shard_count: int, shard_index: int
    ) -> list[dict[str, Any]]:
        count = int(shard_count)
        index = int(shard_index)
        if count < 1 or count > 64 or index < 0 or index >= count:
            raise ValueError("invalid runtime shard")
        rows = self.conn.execute(
            "SELECT b.id AS bot_id, b.tenant_id, b.role, b.encrypted_token,"
            " b.token_fingerprint, b.token_tail, b.telegram_bot_id,"
            " b.telegram_username, t.name AS tenant_name,"
            " t.owner_telegram_id, r.data_namespace"
            " FROM tenant_bots AS b"
            " JOIN tenants AS t ON t.id = b.tenant_id"
            " JOIN tenant_runtime_configs AS r ON r.tenant_id = b.tenant_id"
            " WHERE b.status = 'active' AND b.telegram_bot_id IS NOT NULL"
            " AND b.webhook_secret_hash IS NOT NULL"
            " AND t.status = 'active' AND r.status = 'ready'"
            " AND (b.id % ?) = ? ORDER BY b.id ASC",
            (count, index),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_runtime_candidate(self, bot_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT b.id AS bot_id, b.tenant_id, b.role, b.encrypted_token,"
            " b.token_fingerprint, b.token_tail, b.telegram_bot_id,"
            " b.telegram_username, t.name AS tenant_name,"
            " t.owner_telegram_id, r.data_namespace"
            " FROM tenant_bots AS b"
            " JOIN tenants AS t ON t.id = b.tenant_id"
            " JOIN tenant_runtime_configs AS r ON r.tenant_id = b.tenant_id"
            " WHERE b.id = ? AND b.status = 'active'"
            " AND b.telegram_bot_id IS NOT NULL"
            " AND b.webhook_secret_hash IS NOT NULL"
            " AND t.status = 'active' AND r.status = 'ready'",
            (int(bot_id),),
        ).fetchone()
        return _row_to_dict(row)

    def readiness(self, tenant_id: int) -> dict[str, bool]:
        rows = self.conn.execute(
            "SELECT role FROM tenant_bots WHERE tenant_id = ? AND status = 'active'",
            (int(tenant_id),),
        ).fetchall()
        roles = {str(row["role"]) for row in rows}
        has_admin = "admin" in roles
        has_user = "user" in roles
        return {"admin": has_admin, "user": has_user, "ready": has_admin and has_user}

    def set_telegram_identity(
        self, bot_id: int, *, telegram_bot_id: int, telegram_username: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        if int(telegram_bot_id) <= 0:
            raise ValueError("telegram_bot_id must be positive")
        self.conn.execute(
            "UPDATE tenant_bots SET telegram_bot_id = ?, telegram_username = ?, updated_at = ?"
            " WHERE id = ?",
            (int(telegram_bot_id), telegram_username or None, iso_utc(utcnow()), int(bot_id)),
        )
        return self.get_by_id(int(bot_id))

    def update_status(self, bot_id: int, status: str) -> Optional[dict[str, Any]]:
        if status not in ("active", "disabled", "revoked"):
            raise ValueError("invalid bot status")
        self.conn.execute(
            "UPDATE tenant_bots SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso_utc(utcnow()), int(bot_id)),
        )
        return self.get_by_id(int(bot_id))

    def update_webhook_secret_hash(
        self, bot_id: int, webhook_secret_hash: str
    ) -> Optional[dict[str, Any]]:
        digest = validate_hash(webhook_secret_hash)
        if digest is None:
            raise InvalidBotTokenFieldError("webhook secret hash is required")
        self.conn.execute(
            "UPDATE tenant_bots SET webhook_secret_hash = ?, updated_at = ?"
            " WHERE id = ?",
            (digest, iso_utc(utcnow()), int(bot_id)),
        )
        return self.get_by_id(int(bot_id))

    def rotate_token(
        self, bot_id: int, *, cipher: TokenCipher, plain_token: str
    ) -> Optional[dict[str, Any]]:
        try:
            credential = build_bot_credential(cipher, plain_token)
            encrypted_token = credential["encrypted_token"]
            token_fingerprint = credential["token_fingerprint"]
            token_tail = credential["token_tail"]
            encrypted_token = validate_encrypted_token(encrypted_token)
            token_fingerprint = validate_fingerprint(token_fingerprint)
            token_tail = validate_tail(token_tail)
            if cipher.decrypt(encrypted_token) != plain_token:
                raise ValueError("cipher round-trip mismatch")
        except Exception:
            raise InvalidBotTokenFieldError("invalid bot credential") from None
        self.conn.execute(
            "UPDATE tenant_bots SET encrypted_token = ?, token_fingerprint = ?, token_tail = ?,"
            " telegram_bot_id = NULL, updated_at = ? WHERE id = ?",
            (encrypted_token, token_fingerprint, token_tail, iso_utc(utcnow()), int(bot_id)),
        )
        return self.get_by_id(int(bot_id))


class PlanRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        name: str,
        duration_days: int,
        price: int,
        max_servers: int,
        max_users: int,
        features: Optional[dict[str, Any]] = None,
        status: str = "active",
    ) -> dict[str, Any]:
        if not str(name or "").strip():
            raise ValueError("plan name is required")
        if int(duration_days) <= 0 or int(max_servers) <= 0 or int(max_users) <= 0:
            raise ValueError("duration_days/max_servers/max_users must be positive")
        if int(price) < 0:
            raise ValueError("price must be >= 0")
        if status not in ("active", "archived", "disabled"):
            raise ValueError("invalid plan status")
        cursor = self.conn.execute(
            "INSERT INTO license_plans (name, duration_days, price, max_servers, max_users, features, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name.strip(), int(duration_days), int(price), int(max_servers),
                int(max_users), json.dumps(features or {}, ensure_ascii=False, sort_keys=True),
                status,
            ),
        )
        row = self.get_by_id(int(cursor.lastrowid or 0))
        assert row is not None
        return row

    def get_by_id(self, plan_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM license_plans WHERE id = ?", (int(plan_id),)).fetchone()
        return _row_to_dict(row)

    def list_active(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM license_plans WHERE status = 'active' ORDER BY id ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def list(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM license_plans ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            return []
        like = f"%{text}%"
        if text.isdigit():
            rows = self.conn.execute(
                "SELECT * FROM license_plans WHERE id = ? OR name LIKE ? OR status LIKE ?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (
                    int(text), like, like,
                    max(1, min(int(limit), 100)), max(0, int(offset)),
                ),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM license_plans WHERE name LIKE ? OR status LIKE ?"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                (like, like, max(1, min(int(limit), 100)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update(
        self,
        plan_id: int,
        *,
        name: str,
        duration_days: int,
        price: int,
        max_servers: int,
        max_users: int,
        features: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("plan name is required")
        if int(duration_days) <= 0 or int(max_servers) <= 0 or int(max_users) <= 0:
            raise ValueError("duration_days/max_servers/max_users must be positive")
        if int(price) < 0:
            raise ValueError("price must be >= 0")
        self.conn.execute(
            "UPDATE license_plans SET name = ?, duration_days = ?, price = ?,"
            " max_servers = ?, max_users = ?, features = ? WHERE id = ?",
            (
                clean_name,
                int(duration_days),
                int(price),
                int(max_servers),
                int(max_users),
                json.dumps(features or {}, ensure_ascii=False, sort_keys=True),
                int(plan_id),
            ),
        )
        return self.get_by_id(int(plan_id))

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS total FROM license_plans").fetchone()
        return int(row["total"] if row else 0)

    def update_status(self, plan_id: int, status: str) -> Optional[dict[str, Any]]:
        if status not in ("active", "archived", "disabled"):
            raise ValueError("invalid plan status")
        self.conn.execute("UPDATE license_plans SET status = ? WHERE id = ?", (status, int(plan_id)))
        return self.get_by_id(int(plan_id))


class LicenseRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        tenant_id: int,
        plan_id: int,
        status: str = "pending",
        starts_at: str,
        expires_at: str,
        grace_until: Optional[str] = None,
    ) -> dict[str, Any]:
        if status not in ("pending", "active", "grace", "suspended", "expired", "cancelled"):
            raise ValueError("invalid license status")
        starts, expires, grace = canonical_license_range(starts_at, expires_at, grace_until)
        if status in CURRENT_LICENSE_STATUSES and self.current_usable(int(tenant_id)) is not None:
            raise LicenseConflictError("tenant already holds a current (active/grace) license")
        now = iso_utc(utcnow())
        try:
            cursor = self.conn.execute(
                "INSERT INTO licenses (tenant_id, plan_id, status, starts_at, expires_at, grace_until,"
                " suspended_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (int(tenant_id), int(plan_id), status, starts, expires, grace, now, now),
            )
        except sqlite3.IntegrityError as exc:
            # Re-read to detect whether the failure was a current-license
            # conflict (partial unique index) or a FK/CHECK violation.
            # sqlite_errorname is not consistently available on Python 3.10,
            # so retain a message fallback for the supported runtime matrix.
            existing = self.current_usable(int(tenant_id))
            error_name = str(getattr(exc, "sqlite_errorname", "") or "")
            unique_error = (
                error_name == "SQLITE_CONSTRAINT_UNIQUE"
                or "UNIQUE constraint failed" in str(exc)
            )
            if (
                status in CURRENT_LICENSE_STATUSES
                and unique_error
                and existing is not None
            ):
                raise LicenseConflictError(
                    "tenant already holds a current (active/grace) license"
                ) from exc
            # Not a current-license conflict — re-raise the original FK/CHECK error.
            raise
        row = self.get_by_id(int(cursor.lastrowid or 0))
        assert row is not None
        return row

    def get_by_id(self, license_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM licenses WHERE id = ?", (int(license_id),)).fetchone()
        return _row_to_dict(row)

    def list_by_tenant(self, tenant_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM licenses WHERE tenant_id = ? ORDER BY id DESC", (int(tenant_id),)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_for_evaluation(
        self, *, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM licenses WHERE id > ?"
            " AND status IN ('active', 'grace', 'expired')"
            " ORDER BY id ASC LIMIT ?",
            (max(0, int(after_id)), max(1, min(int(limit), 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM licenses ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            return []
        like = f"%{text}%"
        params: list[Any]
        id_clause = ""
        if text.isdigit():
            id_clause = "l.id = ? OR l.tenant_id = ? OR l.plan_id = ? OR "
            params = [int(text), int(text), int(text)]
        else:
            params = []
        params.extend([like, like, like, like, max(1, min(int(limit), 100)), max(0, int(offset))])
        rows = self.conn.execute(
            "SELECT l.* FROM licenses AS l"
            " JOIN tenants AS t ON t.id = l.tenant_id"
            " JOIN license_plans AS p ON p.id = l.plan_id WHERE "
            + id_clause
            + "t.name LIKE ? OR t.slug LIKE ? OR p.name LIKE ? OR l.status LIKE ?"
            " ORDER BY l.id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_by_tenant(self, tenant_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM licenses WHERE tenant_id = ? ORDER BY id DESC LIMIT 1", (int(tenant_id),)
        ).fetchone()
        return _row_to_dict(row)

    def current_usable(self, tenant_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM licenses WHERE tenant_id = ? AND status IN ('active', 'grace')"
            " ORDER BY id DESC LIMIT 1",
            (int(tenant_id),),
        ).fetchone()
        return _row_to_dict(row)

    def transition(self, license_id: int, *, expected: str, new: str) -> bool:
        if new == "suspended":
            cursor = self.conn.execute(
                "UPDATE licenses SET status = ?, suspended_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (new, iso_utc(utcnow()), iso_utc(utcnow()), int(license_id), expected),
            )
        else:
            cursor = self.conn.execute(
                "UPDATE licenses SET status = ?, suspended_at = NULL, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (new, iso_utc(utcnow()), int(license_id), expected),
            )
        return cursor.rowcount == 1

    def cas_renew(
        self,
        license_id: int,
        *,
        expected_status: str,
        expected_expires_at: str,
        new_expires_at: str,
        new_grace_until: Optional[str],
        new_status: str = "active",
    ) -> bool:
        canonical_license_range("2000-01-01T00:00:00+00:00", new_expires_at, new_grace_until)
        cursor = self.conn.execute(
            "UPDATE licenses SET status = ?, expires_at = ?, grace_until = ?, suspended_at = NULL,"
            " updated_at = ? WHERE id = ? AND status = ? AND expires_at = ?",
            (
                new_status, new_expires_at, new_grace_until, iso_utc(utcnow()),
                int(license_id), expected_status, expected_expires_at,
            ),
        )
        return cursor.rowcount == 1

    # NOTE: No plain `renew()` or `mark_suspended()` exist here.
    # All status changes go through ``LicenseService`` which audits and
    # validates tenant ownership atomically.


class AuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(
        self,
        *,
        actor_id: int,
        tenant_id: Optional[int],
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not str(action or "").strip() or not str(entity_type or "").strip():
            raise ValueError("action and entity_type are required")
        safe_json = sanitize_metadata(metadata)
        safe_action = redact_text(str(action).strip())
        safe_entity_type = redact_text(str(entity_type).strip())
        safe_entity_id = redact_text(str(entity_id).strip())
        now = iso_utc(utcnow())
        cursor = self.conn.execute(
            "INSERT INTO audit_events (actor_id, tenant_id, action, entity_type, entity_id, safe_metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(actor_id), tenant_id, safe_action, safe_entity_type, safe_entity_id, safe_json, now),
        )
        row = self.conn.execute(
            "SELECT * FROM audit_events WHERE id = ?", (int(cursor.lastrowid or 0),)
        ).fetchone()
        assert row is not None
        return dict(row)

    def list_by_tenant(self, tenant_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
            (int(tenant_id), max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 200)), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]


class NotificationRepository:
    """Warning queue with idempotent keys, atomic worker claim and retry support.

    Lifecycle: pending --claim--> processing --sent--> sent
                                        \\--failed--> failed --requeue--> pending
    ``next_attempt_at`` on a failed row tells workers when a retry is due.
    Claim is idempotent (only one worker wins) AND scoped to ``scheduled_at <= now``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(
        self, *, tenant_id: int, event_type: str, event_key: str, scheduled_at: str
    ) -> Optional[dict[str, Any]]:
        """Insert once per event_key. Race-safe via IntegrityError handling.

        If two callers attempt the same ``event_key`` concurrently, the
        second gets an IntegrityError, re-reads, checks tenant ownership
        and returns the (first writer's) row — no cross-tenant leak.
        """
        key = str(event_key or "").strip()
        if not key or not str(event_type or "").strip():
            raise ValueError("event_type and event_key are required")
        try:
            scheduled = iso_utc(parse_utc(str(scheduled_at or "")))
        except Exception as exc:
            raise ValueError("invalid scheduled_at timestamp") from exc
        try:
            self.conn.execute(
                "INSERT INTO notification_events (tenant_id, event_type, event_key, scheduled_at, status, retry_count)"
                " VALUES (?, ?, ?, ?, 'pending', 0)",
                (int(tenant_id), event_type.strip(), key, scheduled),
            )
        except sqlite3.IntegrityError as exc:
            # Only the UNIQUE constraint on event_key indicates a race.
            # Re-read to see if the conflict was indeed a duplicate event_key.
            row = self.conn.execute(
                "SELECT * FROM notification_events WHERE event_key = ?", (key,)
            ).fetchone()
            if row is None:
                # The error was a FK / CHECK violation – re-raise.
                raise
            if int(row["tenant_id"]) != int(tenant_id):
                raise TenantMismatchError("event_key belongs to another tenant") from exc
            return dict(row)
        row = self.conn.execute(
            "SELECT * FROM notification_events WHERE event_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if int(row["tenant_id"]) != int(tenant_id):
            raise TenantMismatchError("event_key belongs to another tenant")
        return dict(row)

    def claim(self, event_key: str, *, now_iso: str) -> bool:
        """Atomically move pending -> processing exclusively,
        only for events whose deadline (``scheduled_at``) has arrived."""
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'processing', locked_at = ?"
            " WHERE event_key = ? AND status = 'pending' AND scheduled_at <= ?",
            (now_iso, str(event_key or "").strip(), now_iso),
        )
        return cursor.rowcount == 1

    def get_by_key(self, event_key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM notification_events WHERE event_key = ?",
            (str(event_key or "").strip(),),
        ).fetchone()
        return _row_to_dict(row)

    def mark_sent(self, event_key: str, *, sent_at: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'sent', sent_at = ?, locked_at = NULL"
            " WHERE event_key = ? AND status IN ('pending', 'processing')",
            (sent_at, str(event_key or "").strip()),
        )
        return cursor.rowcount == 1

    def mark_failed(self, event_key: str, *, next_attempt_at: Optional[str] = None) -> bool:
        deadline: Optional[str] = None
        if next_attempt_at is not None and str(next_attempt_at).strip():
            try:
                deadline = iso_utc(parse_utc(str(next_attempt_at)))
            except Exception as exc:
                raise ValueError("invalid next_attempt_at timestamp") from exc
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'failed', retry_count = retry_count + 1,"
            " next_attempt_at = ?, locked_at = NULL"
            " WHERE event_key = ? AND status IN ('pending', 'processing')",
            (deadline, str(event_key or "").strip()),
        )
        return cursor.rowcount == 1

    def mark_skipped(self, event_key: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'skipped', locked_at = NULL,"
            " next_attempt_at = NULL WHERE event_key = ? AND status = 'processing'",
            (str(event_key or "").strip(),),
        )
        return cursor.rowcount == 1

    def abandon(self, event_key: str) -> bool:
        """Leave an exhausted event failed with no next retry deadline."""
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'failed', locked_at = NULL,"
            " next_attempt_at = NULL WHERE event_key = ? AND status = 'processing'",
            (str(event_key or "").strip(),),
        )
        return cursor.rowcount == 1

    def recover_stale(self, *, stale_before: str, retry_at: str) -> int:
        """Release worker leases left behind by a crashed process."""
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'failed', locked_at = NULL,"
            " next_attempt_at = ?, retry_count = retry_count + 1"
            " WHERE status = 'processing' AND locked_at IS NOT NULL AND locked_at <= ?",
            (retry_at, stale_before),
        )
        return max(0, int(cursor.rowcount))

    def requeue_due(self, *, now_iso: str, limit: int = 100) -> int:
        """Atomically move a bounded set of due failures back to pending."""
        rows = self.conn.execute(
            "SELECT event_key FROM notification_events WHERE status = 'failed'"
            " AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at ASC LIMIT ?",
            (now_iso, max(1, min(int(limit), 500))),
        ).fetchall()
        changed = 0
        for row in rows:
            cursor = self.conn.execute(
                "UPDATE notification_events SET status = 'pending', scheduled_at = ?,"
                " next_attempt_at = NULL, locked_at = NULL"
                " WHERE event_key = ? AND status = 'failed'"
                " AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?",
                (now_iso, str(row["event_key"]), now_iso),
            )
            changed += max(0, int(cursor.rowcount))
        return changed

    def requeue(self, event_key: str, *, not_before: Optional[str] = None) -> bool:
        row = self.conn.execute(
            "SELECT * FROM notification_events WHERE event_key = ?",
            (str(event_key or "").strip(),),
        ).fetchone()
        if row is None or str(row["status"]) != "failed":
            return False
        scheduled = str(row["scheduled_at"])
        if not_before is not None and str(not_before).strip():
            try:
                scheduled = iso_utc(parse_utc(str(not_before)))
            except Exception as exc:
                raise ValueError("invalid not_before timestamp") from exc
        cursor = self.conn.execute(
            "UPDATE notification_events SET status = 'pending', scheduled_at = ?,"
            " next_attempt_at = NULL, locked_at = NULL WHERE event_key = ? AND status = 'failed'",
            (scheduled, str(event_key or "").strip()),
        )
        return cursor.rowcount == 1

    def list_pending(self, *, now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notification_events WHERE status = 'pending' AND scheduled_at <= ?"
            " ORDER BY scheduled_at ASC LIMIT ?",
            (now_iso, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_retryable(self, *, now_iso: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notification_events WHERE status = 'failed'"
            " AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at ASC LIMIT ?",
            (now_iso, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_open(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notification_events WHERE status IN ('pending', 'processing', 'failed')"
            " ORDER BY scheduled_at ASC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 200)), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]
