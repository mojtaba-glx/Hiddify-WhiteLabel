"""Tenant-scoped administration of live panel users and their child nodes.

SQLite stays on the event-loop thread; provider HTTP is dispatched to workers.
An authoritative inventory never manufactures customer/payment/order records.
"""

from __future__ import annotations

from TenantRuntime.panels import decode_panel_note

import asyncio
import json
import math
import uuid
from dataclasses import asdict
from datetime import timedelta

from Database.connection import transaction
from Shared.timeutils import iso_utc, parse_utc, utcnow
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.panels import PanelError, ProvisionRequest, RenewRequest
from TenantRuntime.server_connections import url

DEFAULT_SALES = dict(
    mode="fixed",
    min_gb=10,
    max_gb=1000,
    step_gb=10,
    min_days=30,
    max_days=365,
    step_days=30,
    price_gb=0,
    price_day=0,
    discount_percent=0,
    currency="IRR",
    discount_simple_enabled=False,
    discount_tiered_enabled=False,
    discount_step_gb=50,
    discount_percent_step=5,
    discount_percent_max=30,
    discount_tiers=[],
    discount_simple_until=None,
    discount_tiered_until=None,
)


def user_status(row):
    if row.get("state") == "pending":
        return "pending"
    if row.get("state") == "deleted":
        return "deleted"
    due = bool(
        row.get("traffic_bytes")
        and int(row.get("usage_bytes") or 0) >= int(row["traffic_bytes"])
    )
    if row.get("expires_at"):
        try:
            due = due or parse_utc(row["expires_at"]) <= utcnow()
        except (ValueError, TypeError):
            pass
    return "expired" if due else "active" if row.get("active") else "disabled"


class ServerAdminService:
    def __init__(self, business):
        self.b, self.conn, self.tenant_id = business, business.conn, business.tenant_id

    def authorize(self, actor, sid):
        self.b._admin(actor)
        return self.b.server(int(sid))

    async def call(self, sid, method, **kwargs):
        _, target, secret = self.b._panel_material(int(sid))
        function = getattr(self.b.panel_adapter, method, None)
        if not callable(function):
            raise TenantBusinessError("panel operation is unavailable")
        try:
            return await asyncio.to_thread(
                function, target=target, secret=secret, **kwargs
            )
        except PanelError as exc:
            raise TenantBusinessError("panel operation failed") from exc
        finally:
            secret = ""

    def save_user(self, sid, data, *, extra=None):
        ref = str(data.get("external_ref") or "").strip()
        if not ref or len(ref) > 255:
            raise TenantBusinessError("invalid panel user identity")
        name, comment = (
            str(data.get("name") or ref)[:80],
            str(data.get("comment") or "")[:2000],
        )
        try:
            note = decode_panel_note(comment)
            if isinstance(note, dict):
                name, comment = (
                    str(note.get("name") or name)[:80],
                    str(note.get("note") or "")[:2000],
                )
        except (ValueError, TypeError):
            pass
        extra = dict(extra or {})
        if "online" in data:
            extra["online"] = data["online"]
        self.conn.execute(
            "INSERT INTO tenant_panel_users (tenant_id,server_id,external_ref,name,comment,usage_bytes,"
            "traffic_bytes,expires_at,last_online,active,state,extra_json,last_synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?) ON CONFLICT(tenant_id,server_id,external_ref) "
            "DO UPDATE SET name=excluded.name,comment=excluded.comment,usage_bytes=excluded.usage_bytes,"
            "traffic_bytes=excluded.traffic_bytes,expires_at=excluded.expires_at,last_online=excluded.last_online,"
            "active=excluded.active,state='active',extra_json=json_patch(extra_json,?),last_synced_at=excluded.last_synced_at",
            (
                self.tenant_id,
                int(sid),
                ref,
                name,
                comment,
                max(0, int(data.get("usage_bytes") or 0)),
                data.get("traffic_bytes"),
                data.get("expires_at"),
                data.get("last_online"),
                int(bool(data.get("active"))),
                json.dumps(extra or {}, ensure_ascii=False),
                iso_utc(utcnow()),
                json.dumps(extra),
            ),
        )

    async def refresh_users(self, actor, sid):
        self.authorize(actor, sid)
        rows = await self.call(sid, "list_users")
        if not isinstance(rows, list):
            raise TenantBusinessError("invalid panel inventory")
        # Validate the complete response before marking any cached user absent.
        refs = [
            str(x.get("external_ref") or "").strip()
            for x in rows
            if isinstance(x, dict)
        ]
        if (
            len(refs) != len(rows)
            or any(not x or len(x) > 255 for x in refs)
            or len(set(refs)) != len(refs)
        ):
            raise TenantBusinessError("invalid panel inventory")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_panel_users SET state='deleted' WHERE tenant_id=? AND server_id=? AND state='active'",
                (self.tenant_id, int(sid)),
            )
            for position, row in enumerate(rows):
                self.save_user(
                    sid,
                    row,
                    extra={"list_position": position}
                    | {k: row[k] for k in ("duration_days", "start_date") if k in row},
                )
        return self.users(actor, sid)

    def users(self, actor, sid, *, query="", status="all"):
        self.authorize(actor, sid)
        rows = [
            dict(x)
            for x in self.conn.execute(
                "SELECT * FROM tenant_panel_users WHERE tenant_id=? AND server_id=? AND state!='deleted' ORDER BY id DESC",
                (self.tenant_id, int(sid)),
            ).fetchall()
        ]
        needle = str(query).strip().casefold()
        if needle:
            rows = [
                r
                for r in rows
                if any(
                    needle in str(r[k]).casefold()
                    for k in ("name", "external_ref", "comment", "id")
                )
            ]
        return [r for r in rows if status == "all" or user_status(r) == status]

    def user(self, actor, sid, uid):
        self.authorize(actor, sid)
        row = self.conn.execute(
            "SELECT * FROM tenant_panel_users WHERE id=? AND tenant_id=? AND server_id=? AND state!='deleted'",
            (int(uid), self.tenant_id, int(sid)),
        ).fetchone()
        if row is None:
            raise TenantBusinessError("user not found on this server")
        result = dict(row)
        sub = self.conn.execute(
            "SELECT s.id FROM tenant_subscriptions s LEFT JOIN tenant_subscription_nodes n "
            "ON n.tenant_id=s.tenant_id AND n.subscription_id=s.id "
            "WHERE s.tenant_id=? AND ((s.server_id=? AND s.external_ref=?) OR (n.server_id=? AND n.external_ref=?)) LIMIT 1",
            (
                self.tenant_id,
                int(sid),
                result["external_ref"],
                int(sid),
                result["external_ref"],
            ),
        ).fetchone()
        result["subscription_id"] = int(sub["id"]) if sub else None
        return result

    async def live_user(self, actor, sid, uid):
        row = self.user(actor, sid, uid)
        data = await self.call(sid, "get_user", external_ref=row["external_ref"])
        with transaction(self.conn):
            self.save_user(sid, asdict(data))
        return self.user(actor, sid, uid)

    def reset_duration(self, actor, sid, uid):
        row = self.user(actor, sid, uid)
        if row["subscription_id"]:
            sub = self.b._admin_subscription(actor, row["subscription_id"])
            plan = self.b.plan(sub["plan_id"], public=False)
            return max(1, int(plan["duration_days"]))
        original = json.loads(row["extra_json"]).get("duration_days")
        if original:
            return max(1, int(original))
        if row.get("expires_at"):
            return max(
                1,
                math.ceil(
                    (parse_utc(row["expires_at"]) - utcnow()).total_seconds() / 86400
                ),
            )
        return 30

    def _adopt_created_identity(
        self,
        sid: int,
        uid: int,
        *,
        requested_ref: str,
        panel_ref: str,
        duration_days: int,
    ) -> int:
        actual = str(panel_ref or "").strip()
        requested = str(requested_ref or "").strip()
        if not actual or len(actual) > 255:
            raise TenantBusinessError("invalid panel user identity")

        meta = json.dumps(
            {
                "duration_days": int(duration_days),
                "create_request_ref": requested,
            },
            ensure_ascii=False,
        )
        if actual == requested:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_panel_users "
                    "SET extra_json=json_patch(extra_json, ?) "
                    "WHERE id=? AND tenant_id=? AND server_id=?",
                    (meta, int(uid), self.tenant_id, int(sid)),
                )
            return int(uid)

        collision = self.conn.execute(
            "SELECT id FROM tenant_panel_users "
            "WHERE tenant_id=? AND server_id=? AND external_ref=? AND id<>? "
            "LIMIT 1",
            (self.tenant_id, int(sid), actual, int(uid)),
        ).fetchone()
        with transaction(self.conn):
            if collision is not None:
                adopted_uid = int(collision["id"])
                # The remote row may already be present from a previous refresh.
                # Keep it as the canonical record and retire only our pending
                # placeholder.  Never manufacture a second local user.
                self.conn.execute(
                    "UPDATE tenant_panel_users SET state='deleted' "
                    "WHERE id=? AND tenant_id=? AND server_id=?",
                    (int(uid), self.tenant_id, int(sid)),
                )
                self.conn.execute(
                    "UPDATE tenant_panel_users "
                    "SET extra_json=json_patch(extra_json, ?) "
                    "WHERE id=? AND tenant_id=? AND server_id=?",
                    (meta, adopted_uid, self.tenant_id, int(sid)),
                )
                return adopted_uid

            self.conn.execute(
                "UPDATE tenant_panel_users "
                "SET external_ref=?, extra_json=json_patch(extra_json, ?) "
                "WHERE id=? AND tenant_id=? AND server_id=?",
                (actual, meta, int(uid), self.tenant_id, int(sid)),
            )
        return int(uid)

    async def _recover_created_identity(
        self,
        sid: int,
        *,
        requested_ref: str,
        uid: int,
    ) -> str | None:
        requested = str(requested_ref or "").strip()
        try:
            recovered = await self.call(
                sid, "get_user", external_ref=requested
            )
            actual = str(recovered.external_ref or "").strip()
            if actual:
                return actual
        except TenantBusinessError:
            pass

        marker = (
            f"WhiteLabel tenant={int(self.tenant_id)} "
            f"subscription={-int(uid)}"
        )
        try:
            rows = await self.call(sid, "list_users")
        except TenantBusinessError:
            return None
        if not isinstance(rows, list):
            return None

        matches: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            actual = str(row.get("external_ref") or "").strip()
            if not actual:
                continue
            comment = str(row.get("comment") or "")
            if actual == requested or marker in comment:
                matches.append(actual)
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    async def create_users(
        self, actor, sid, *, name, gb, days, count=1, operation_key, comment=""
    ):
        self.authorize(actor, sid)
        if (
            not 1 <= int(count) <= 100
            or not 0 < float(gb) <= 1000000
            or not 1 <= int(days) <= 36500
        ):
            raise ValueError("invalid user package")
        clean = str(name).strip()
        if not 1 <= len(clean) <= 64 or "\n" in clean:
            raise ValueError("invalid user name")

        results, errors = [], 0
        error_details: list[str] = []
        traffic_bytes = int(float(gb) * 1024**3)
        expires_at = iso_utc(utcnow() + timedelta(days=int(days)))

        for index in range(int(count)):
            requested_ref = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"wl-admin:{self.tenant_id}:{sid}:{operation_key}:{index}",
                )
            )
            label = clean if count == 1 else f"{clean}-{index+1}"
            existing = self.conn.execute(
                "SELECT * FROM tenant_panel_users "
                "WHERE tenant_id=? AND server_id=? AND state!='deleted' "
                "AND (external_ref=? OR "
                "json_extract(extra_json,'$.create_request_ref')=?) "
                "ORDER BY CASE WHEN external_ref=? THEN 0 ELSE 1 END LIMIT 1",
                (
                    self.tenant_id,
                    int(sid),
                    requested_ref,
                    requested_ref,
                    requested_ref,
                ),
            ).fetchone()
            if existing and existing["state"] == "active":
                results.append(dict(existing))
                continue

            initial_meta = json.dumps(
                {
                    "duration_days": int(days),
                    "create_request_ref": requested_ref,
                },
                ensure_ascii=False,
            )
            if existing is None:
                with transaction(self.conn):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO tenant_panel_users "
                        "(tenant_id,server_id,external_ref,name,state,extra_json) "
                        "VALUES (?,?,?,?,'pending',?)",
                        (
                            self.tenant_id,
                            int(sid),
                            requested_ref,
                            label,
                            initial_meta,
                        ),
                    )
                existing = self.conn.execute(
                    "SELECT * FROM tenant_panel_users "
                    "WHERE tenant_id=? AND server_id=? AND external_ref=?",
                    (self.tenant_id, int(sid), requested_ref),
                ).fetchone()
            if existing is None:
                errors += 1
                error_details.append(f"{label}: ثبت موقت کاربر ناموفق بود")
                continue

            uid = int(existing["id"])
            request = ProvisionRequest(
                tenant_id=self.tenant_id,
                server_id=int(sid),
                subscription_id=-int(uid),
                customer_id=0,
                traffic_bytes=traffic_bytes,
                duration_days=int(days),
                expires_at=expires_at,
                idempotency_key=f"admin:{operation_key}:{index}",
                external_ref=requested_ref,
                name=label,
            )

            panel_ref = requested_ref
            try:
                created = await self.call(
                    sid,
                    "provision",
                    request=request,
                )
                panel_ref = str(created.external_ref or "").strip()
                if not panel_ref:
                    raise TenantBusinessError(
                        "panel returned an empty user identity"
                    )
            except TenantBusinessError:
                # A Hiddify POST may already have created the user even when
                # the follow-up response/stabilization step fails.  Recover by
                # deterministic UUID first, then by the unique SellBot-style
                # tenant/subscription marker embedded in the remote comment.
                recovered_ref = await self._recover_created_identity(
                    sid,
                    requested_ref=requested_ref,
                    uid=uid,
                )
                if not recovered_ref:
                    errors += 1
                    error_details.append(f"{label}: ساخت روی پنل تأیید نشد")
                    continue
                panel_ref = recovered_ref

            try:
                uid = self._adopt_created_identity(
                    int(sid),
                    uid,
                    requested_ref=requested_ref,
                    panel_ref=panel_ref,
                    duration_days=int(days),
                )
            except (TenantBusinessError, sqlite3.IntegrityError):
                errors += 1
                error_details.append(f"{label}: شناسه برگشتی پنل قابل ثبت نبود")
                continue
            ref = panel_ref

            # The create request already carries the final name/package.  Do
            # not require an extra PATCH just to rewrite the same name.
            note = str(comment or "").strip()
            remote = None
            if note:
                try:
                    remote = await self.call(
                        sid,
                        "update_user",
                        external_ref=ref,
                        changes={"comment": note[:1000]},
                    )
                except TenantBusinessError:
                    remote = None

            if remote is None:
                data = {
                    "external_ref": ref,
                    "name": label,
                    "comment": note[:1000],
                    "usage_bytes": 0,
                    "traffic_bytes": traffic_bytes,
                    "expires_at": expires_at,
                    "last_online": None,
                    "active": True,
                }
                try:
                    fetched = await self.call(
                        sid, "get_user", external_ref=ref
                    )
                except TenantBusinessError:
                    fetched = None
                if fetched is not None:
                    data = asdict(fetched)
                    if note and not str(data.get("comment") or "").strip():
                        data["comment"] = note[:1000]
            else:
                data = asdict(remote)

            with transaction(self.conn):
                self.save_user(
                    sid,
                    data,
                    extra={
                        "duration_days": int(days),
                        "create_request_ref": requested_ref,
                    },
                )
            results.append(self.user(actor, sid, uid))

        node_errors = 0
        node_success_labels: list[str] = []
        node_failed_labels: list[str] = []
        attached_nodes = [
            node
            for node in self.b.list_nodes(parent_server_id=int(sid))
            if node.get("status") == "active"
            and node.get("server_id")
            and int(node["server_id"]) != int(sid)
        ]
        if results and attached_nodes:
            for user in results:
                sync = await self.sync_nodes(
                    actor,
                    int(sid),
                    "missing",
                    only_user=int(user["id"]),
                )
                node_errors += int(sync.get("errors") or 0)

            for node in attached_nodes:
                target_sid = int(node["server_id"])
                healthy = True
                for user in results:
                    mapping = self.conn.execute(
                        "SELECT external_ref,last_error,frozen_at "
                        "FROM tenant_panel_user_nodes "
                        "WHERE tenant_id=? AND source_user_id=? AND server_id=?",
                        (self.tenant_id, int(user["id"]), target_sid),
                    ).fetchone()
                    if (
                        mapping is None
                        or not str(mapping["external_ref"] or "").strip()
                        or str(mapping["last_error"] or "").strip()
                        or mapping["frozen_at"]
                    ):
                        healthy = False
                        break
                label = str(
                    node.get("label")
                    or self.b.server(target_sid).get("label")
                    or f"Server {target_sid}"
                ).strip()
                if healthy:
                    node_success_labels.append(label)
                else:
                    node_failed_labels.append(label)

        return {
            "users": results,
            "errors": errors,
            "error_details": error_details,
            "node_errors": node_errors,
            "node_success_labels": node_success_labels,
            "node_failed_labels": node_failed_labels,
        }

    def related_targets(self, actor, sid, uid):
        row = self.user(actor, sid, uid)
        targets = [(int(sid), row["external_ref"])]
        if not row["subscription_id"]:
            origin = self.conn.execute(
                "SELECT u.id,u.server_id FROM tenant_panel_user_nodes n JOIN tenant_panel_users u ON u.id=n.source_user_id AND u.tenant_id=n.tenant_id WHERE n.tenant_id=? AND n.server_id=? AND n.external_ref=? AND u.state!='deleted' LIMIT 1",
                (self.tenant_id, int(sid), row["external_ref"]),
            ).fetchone()
            if origin:
                row["_source_user_id"] = int(origin["id"])
                targets = [
                    (
                        int(origin["server_id"]),
                        self.user(actor, origin["server_id"], origin["id"])[
                            "external_ref"
                        ],
                    )
                ] + [
                    (int(n["server_id"]), str(n["external_ref"]))
                    for n in self.conn.execute(
                        "SELECT server_id,external_ref FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=?",
                        (self.tenant_id, int(origin["id"])),
                    ).fetchall()
                ]
        if row["subscription_id"]:
            self.b._require_completed_rotation(row["subscription_id"])
            sub = self.b._admin_subscription(actor, row["subscription_id"])
            self.b._ensure_primary_subscription_node(sub)
            targets = [
                (int(n["server_id"]), str(n["external_ref"]))
                for n in self.conn.execute(
                    "SELECT server_id,external_ref FROM tenant_subscription_nodes WHERE tenant_id=? AND subscription_id=? AND external_ref IS NOT NULL",
                    (self.tenant_id, row["subscription_id"]),
                ).fetchall()
            ]
        elif "_source_user_id" not in row:
            targets += [
                (int(n["server_id"]), str(n["external_ref"]))
                for n in self.conn.execute(
                    "SELECT server_id,external_ref FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=?",
                    (self.tenant_id, int(uid)),
                ).fetchall()
            ]
        return row, list(dict.fromkeys(targets))

    async def edit_user(self, actor, sid, uid, changes):
        row, targets = self.related_targets(actor, sid, uid)
        allowed = {
            "name",
            "comment",
            "traffic_bytes",
            "expires_at",
            "reset_usage",
            "reset_days",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("invalid user edit")
        if "name" in changes and not 1 <= len(str(changes["name"]).strip()) <= 64:
            raise ValueError("invalid user name")
        if "traffic_bytes" in changes and int(changes["traffic_bytes"]) < 0:
            raise ValueError("invalid quota")
        if row["subscription_id"] and any(
            k in changes
            for k in ("traffic_bytes", "expires_at", "reset_usage", "reset_days")
        ):
            if "traffic_bytes" in changes and int(changes["traffic_bytes"]) % 1024**3:
                raise ValueError("managed quotas require whole gigabytes")
            gb = (
                int(changes["traffic_bytes"]) // 1024**3
                if "traffic_bytes" in changes
                else None
            )
            days = (
                max(
                    1,
                    math.ceil(
                        (parse_utc(changes["expires_at"]) - utcnow()).total_seconds()
                        / 86400
                    ),
                )
                if "expires_at" in changes
                else None
            )
            self.b.edit_subscription_terms_admin(
                actor,
                subscription_id=row["subscription_id"],
                traffic_gb=gb,
                duration_days=days,
                reset_usage=bool(changes.get("reset_usage")),
                reset_days=bool(changes.get("reset_days")),
            )
        else:
            for target, ref in targets:
                data = await self.call(
                    target, "update_user", external_ref=ref, changes=changes
                )
                with transaction(self.conn):
                    self.save_user(target, asdict(data))
        if row["subscription_id"] and "name" in changes:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_subscriptions SET service_name=?,updated_at=? WHERE tenant_id=? AND id=?",
                    (
                        str(changes["name"]).strip(),
                        iso_utc(utcnow()),
                        self.tenant_id,
                        row["subscription_id"],
                    ),
                )
        if changes.get("reset_days") and changes.get("expires_at"):
            duration = max(
                1,
                math.ceil(
                    (parse_utc(changes["expires_at"]) - utcnow()).total_seconds()
                    / 86400
                ),
            )
            with transaction(self.conn):
                for target, ref in targets:
                    self.conn.execute(
                        "UPDATE tenant_panel_users SET extra_json=json_set(extra_json,'$.duration_days',?) WHERE tenant_id=? AND server_id=? AND external_ref=?",
                        (duration, self.tenant_id, target, ref),
                    )
        return await self.live_user(actor, sid, uid)

    async def toggle_user(self, actor, sid, uid):
        row, targets = self.related_targets(actor, sid, uid)
        enabled = not bool(row["active"])
        if enabled and user_status(row) == "expired":
            raise TenantBusinessError("expired user must be renewed first")
        if row["subscription_id"]:
            self.b.set_subscription_enabled(
                actor, subscription_id=row["subscription_id"], enabled=enabled
            )
        else:
            for target, ref in targets:
                data = await self.call(
                    target, "set_enabled", external_ref=ref, enabled=enabled
                )
                with transaction(self.conn):
                    self.save_user(target, asdict(data))
        return await self.live_user(actor, sid, uid)

    async def delete_user(self, actor, sid, uid, *, all_targets=False):
        row, targets = self.related_targets(actor, sid, uid)
        if (
            row["subscription_id"]
            and not all_targets
            and int(
                self.b._admin_subscription(actor, row["subscription_id"])["server_id"]
            )
            == int(sid)
        ):
            raise TenantBusinessError("primary managed user requires full deletion")
        if row["subscription_id"] and all_targets:
            self.b.delete_subscription_from_panel(
                actor, subscription_id=row["subscription_id"]
            )
        else:
            for target, ref in (
                targets if all_targets else [(int(sid), row["external_ref"])]
            ):
                await self.call(target, "delete_user", external_ref=ref)
                with transaction(self.conn):
                    self.conn.execute(
                        "UPDATE tenant_panel_users SET state='deleted',active=0 WHERE tenant_id=? AND server_id=? AND external_ref=?",
                        (self.tenant_id, target, ref),
                    )
                    if row["subscription_id"]:
                        self.conn.execute(
                            "UPDATE tenant_subscription_nodes SET status='error',last_error='deleted by admin',frozen_at=?,updated_at=? WHERE tenant_id=? AND subscription_id=? AND server_id=?",
                            (
                                iso_utc(utcnow()),
                                iso_utc(utcnow()),
                                self.tenant_id,
                                row["subscription_id"],
                                target,
                            ),
                        )
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_panel_users SET state='deleted',active=0 WHERE tenant_id=? AND id=?",
                (self.tenant_id, int(uid)),
            )
            if all_targets:
                for target, ref in targets:
                    self.conn.execute(
                        "UPDATE tenant_panel_users SET state='deleted',active=0 WHERE tenant_id=? AND server_id=? AND external_ref=?",
                        (self.tenant_id, target, ref),
                    )
                self.conn.execute(
                    "DELETE FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=?",
                    (self.tenant_id, int(row.get("_source_user_id", uid))),
                )

    def node(self, actor, parent, nid):
        self.authorize(actor, parent)
        matches = [
            n
            for n in self.b.list_nodes(parent_server_id=int(parent))
            if int(n["id"]) == int(nid) and n.get("server_id") != int(parent)
        ]
        if not matches:
            raise TenantBusinessError("node does not belong to this server")
        return matches[0]

    def attach_node(self, actor, parent, target):
        self.authorize(actor, parent)
        child = self.authorize(actor, target)
        self.b._purchase_server(int(target))
        nodes = self.b.list_nodes()
        if int(target) == int(parent) or any(
            n["server_id"] == int(target) and n.get("parent_server_id") is not None
            for n in nodes
        ):
            raise TenantBusinessError("duplicate node")
        graph = {}
        for n in nodes:
            if n["parent_server_id"] and n["server_id"]:
                graph.setdefault(int(n["parent_server_id"]), set()).add(
                    int(n["server_id"])
                )
        seen, todo = set(), [int(target)]
        while todo:
            current = todo.pop()
            if current == int(parent):
                raise TenantBusinessError("node cycle")
            if current not in seen:
                seen.add(current)
                todo.extend(graph.get(current, set()))
        return self.b.add_node(
            actor,
            label=str(child["label"]),
            server_id=int(target),
            parent_server_id=int(parent),
        )

    async def sync_nodes(
        self, actor, parent, mode="report", *, only_user=None, only_node=None
    ):
        self.authorize(actor, parent)
        if mode not in {
            "report",
            "missing",
            "details",
            "status",
            "full",
            "extra",
            "migrate",
        }:
            raise ValueError("invalid sync mode")
        source = await self.refresh_users(actor, parent)
        if only_user is not None:
            source = [x for x in source if x["id"] == int(only_user)]
        nodes = [
            n
            for n in self.b.list_nodes(parent_server_id=int(parent))
            if n["status"] == "active"
            and n.get("server_id")
            and n["server_id"] != int(parent)
        ]
        if only_node is not None:
            nodes = [n for n in nodes if n["id"] == int(only_node)]
        summary = dict(
            source=len(source),
            nodes=len(nodes),
            missing=0,
            existing=0,
            created=0,
            updated=0,
            errors=0,
            extras=[],
        )
        if mode == "migrate":
            # Native admin services have their own inventory records; importing
            # them never invents customers, sales or paid orders.
            for node in nodes:
                sid = int(node["server_id"])
                try:
                    inventory = await self.refresh_users(actor, sid)
                except TenantBusinessError:
                    summary["errors"] += 1
                    continue
                known = {r["external_ref"]: r for r in inventory}
                for user in source:
                    managed = self.user(actor, parent, user["id"])
                    if managed["subscription_id"] or user["external_ref"] not in known:
                        continue
                    with transaction(self.conn):
                        self.conn.execute(
                            "INSERT INTO tenant_panel_user_nodes (tenant_id,source_user_id,server_id,external_ref,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,source_user_id,server_id) DO UPDATE SET external_ref=excluded.external_ref,last_error=NULL,fail_count=0,frozen_at=NULL,updated_at=excluded.updated_at",
                            (
                                self.tenant_id,
                                user["id"],
                                sid,
                                user["external_ref"],
                                iso_utc(utcnow()),
                            ),
                        )
                    summary["existing"] += 1
            summary["registered"] = len(source)
            return summary
        for node in nodes:
            sid = int(node["server_id"])
            try:
                inventory = await self.refresh_users(actor, sid)
            except TenantBusinessError:
                summary["errors"] += 1
                if mode not in {"report", "extra"}:
                    with transaction(self.conn):
                        for user in source:
                            self.conn.execute(
                                "INSERT INTO tenant_panel_user_nodes (tenant_id,source_user_id,server_id,external_ref,last_error,fail_count,frozen_at,updated_at) VALUES (?,?,?,?,?,1,?,?) "
                                "ON CONFLICT(tenant_id,source_user_id,server_id) DO UPDATE SET last_error=excluded.last_error,fail_count=fail_count+1,frozen_at=COALESCE(frozen_at,excluded.frozen_at),updated_at=excluded.updated_at",
                                (
                                    self.tenant_id,
                                    user["id"],
                                    sid,
                                    user["external_ref"],
                                    "node inventory unavailable",
                                    iso_utc(utcnow()),
                                    iso_utc(utcnow()),
                                ),
                            )
                continue
            known = {r["external_ref"]: r for r in inventory}
            expected = set()
            for user in source:
                managed = self.user(actor, parent, user["id"])
                mapped = None
                if managed["subscription_id"]:
                    try:
                        self.b._require_completed_rotation(managed["subscription_id"])
                    except TenantBusinessError:
                        summary["errors"] += 1
                        continue
                    mapped = self.conn.execute(
                        "SELECT external_ref FROM tenant_subscription_nodes WHERE tenant_id=? AND subscription_id=? AND server_id=?",
                        (self.tenant_id, managed["subscription_id"], sid),
                    ).fetchone()
                else:
                    mapped = self.conn.execute(
                        "SELECT external_ref FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=? AND server_id=?",
                        (self.tenant_id, user["id"], sid),
                    ).fetchone()
                ref = (
                    str(mapped["external_ref"])
                    if mapped and mapped["external_ref"]
                    else user["external_ref"]
                )
                expected.add(ref)
                present = ref in known
                summary["existing" if present else "missing"] += 1
                if mode in {"report", "extra"}:
                    continue
                try:
                    created = False
                    if not present and mode in {"missing", "full"}:
                        expiry = user.get("expires_at")
                        extra = json.loads(user["extra_json"])
                        days = (
                            max(
                                1,
                                math.ceil(
                                    (parse_utc(expiry) - utcnow()).total_seconds()
                                    / 86400
                                ),
                            )
                            if expiry
                            else max(1, int(extra.get("duration_days") or 30))
                        )
                        provisioned = await self.call(
                            sid,
                            "provision",
                            request=ProvisionRequest(
                                self.tenant_id,
                                sid,
                                -user["id"],
                                0,
                                int(user["traffic_bytes"] or 0),
                                days,
                                expiry or iso_utc(utcnow() + timedelta(days=days)),
                                f'node:{parent}:{user["id"]}:{sid}',
                                external_ref=ref,
                                name=user["name"],
                            ),
                        )
                        if provisioned.external_ref != ref:
                            raise TenantBusinessError(
                                "node returned a different identity"
                            )
                        present = created = True
                        summary["created"] += 1
                    if not present:
                        continue
                    if created or mode in {"details", "full"}:
                        changes = {"traffic_bytes": int(user["traffic_bytes"] or 0)}
                        if created:
                            changes["name"] = user["name"]
                        if user.get("expires_at"):
                            changes["expires_at"] = user["expires_at"]
                        elif json.loads(user["extra_json"]).get("duration_days"):
                            changes["duration_days"] = int(
                                json.loads(user["extra_json"])["duration_days"]
                            )
                            changes["start_date"] = None
                        await self.call(
                            sid, "update_user", external_ref=ref, changes=changes
                        )
                        summary["updated"] += 1
                    if created or mode == "status":
                        await self.call(
                            sid,
                            "set_enabled",
                            external_ref=ref,
                            enabled=user_status(user) == "active",
                        )
                    data = await self.call(sid, "get_user", external_ref=ref)
                    with transaction(self.conn):
                        self.save_user(sid, asdict(data))
                        if managed["subscription_id"]:
                            self.conn.execute(
                                "DELETE FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=? AND server_id=?",
                                (self.tenant_id, user["id"], sid),
                            )
                            self.b._upsert_subscription_node(
                                subscription_id=managed["subscription_id"],
                                server_id=sid,
                                external_ref=ref,
                                is_primary=False,
                                status="active" if data.active else "disabled",
                                usage_bytes=data.usage_bytes,
                                last_online=data.last_online,
                            )
                        else:
                            self.conn.execute(
                                "INSERT INTO tenant_panel_user_nodes (tenant_id,source_user_id,server_id,external_ref,updated_at) VALUES (?,?,?,?,?) "
                                "ON CONFLICT(tenant_id,source_user_id,server_id) DO UPDATE SET external_ref=excluded.external_ref,last_error=NULL,fail_count=0,frozen_at=NULL,updated_at=excluded.updated_at",
                                (
                                    self.tenant_id,
                                    user["id"],
                                    sid,
                                    ref,
                                    iso_utc(utcnow()),
                                ),
                            )
                except (TenantBusinessError, ValueError, TypeError):
                    summary["errors"] += 1
                    with transaction(self.conn):
                        self.conn.execute(
                            "INSERT INTO tenant_panel_user_nodes (tenant_id,source_user_id,server_id,external_ref,last_error,fail_count,frozen_at,updated_at) VALUES (?,?,?,?,?,1,?,?) "
                            "ON CONFLICT(tenant_id,source_user_id,server_id) DO UPDATE SET last_error=excluded.last_error,fail_count=fail_count+1,frozen_at=COALESCE(frozen_at,excluded.frozen_at),updated_at=excluded.updated_at",
                            (
                                self.tenant_id,
                                user["id"],
                                sid,
                                ref,
                                "node sync failed",
                                iso_utc(utcnow()),
                                iso_utc(utcnow()),
                            ),
                        )
            summary["extras"] += [
                {"server_id": sid, "name": r["name"], "id": r["id"]}
                for ref, r in known.items()
                if ref not in expected
            ]
        return summary

    def sales(self, sid):
        self.b.server(int(sid))
        row = self.conn.execute(
            "SELECT settings_json FROM tenant_server_sales_settings WHERE tenant_id=? AND server_id=?",
            (self.tenant_id, int(sid)),
        ).fetchone()
        return DEFAULT_SALES | (json.loads(row["settings_json"]) if row else {})

    def set_sales(self, actor, sid, changes):
        self.authorize(actor, sid)
        if set(changes) - set(DEFAULT_SALES):
            raise ValueError("invalid sales setting")
        settings = self.sales(sid) | changes
        if settings["mode"] not in {"fixed", "dynamic", "mixed"} or settings[
            "currency"
        ] not in {"IRR", "IRT", "USD", "USDT", "EUR"}:
            raise ValueError("invalid sales setting")
        for key in set(DEFAULT_SALES) - {
            "mode",
            "currency",
            "discount_tiers",
            "discount_simple_until",
            "discount_tiered_until",
            "discount_simple_enabled",
            "discount_tiered_enabled",
        }:
            settings[key] = int(settings[key])
            if settings[key] < 0:
                raise ValueError("invalid sales setting")
        if (
            not 0 < settings["min_gb"] <= settings["max_gb"] <= 1000000
            or not 0 < settings["min_days"] <= settings["max_days"] <= 36500
            or min(settings["step_gb"], settings["step_days"]) < 1
            or settings["discount_percent"] > 100
        ):
            raise ValueError("invalid dynamic limits")
        for key in ("discount_simple_enabled", "discount_tiered_enabled"):
            if not isinstance(settings[key], bool):
                raise ValueError("invalid discount state")
        for key in ("discount_simple_until", "discount_tiered_until"):
            if settings[key] is not None:
                parse_utc(settings[key])
        if (
            settings["discount_step_gb"] < 1
            or max(settings["discount_percent_step"], settings["discount_percent_max"])
            > 100
        ):
            raise ValueError("invalid volume discount")
        tiers = settings["discount_tiers"]
        if not isinstance(tiers, list) or len(tiers) > 50:
            raise ValueError("invalid discount tiers")
        normalized = []
        for t in tiers:
            gb, percent = int(t["gb"]), int(t["percent"])
            if not 0 < gb <= 1000000 or not 0 <= percent <= 100:
                raise ValueError("invalid discount tier")
            normalized.append(dict(gb=gb, percent=percent))
        if len({t["gb"] for t in normalized}) != len(normalized):
            raise ValueError("duplicate discount threshold")
        settings["discount_tiers"] = sorted(normalized, key=lambda t: t["gb"])
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tenant_server_sales_settings (tenant_id,server_id,settings_json) VALUES (?,?,?) ON CONFLICT(tenant_id,server_id) DO UPDATE SET settings_json=excluded.settings_json",
                (self.tenant_id, int(sid), json.dumps(settings)),
            )
        return settings

    def plans(self, actor, sid):
        self.authorize(actor, sid)
        return [
            p
            for p in self.b.list_plans(public=False, server_id=int(sid))
            if not p["name"].startswith("__WHITELABEL_")
            and p["status"] != "archived"
            and not p.get("is_dynamic")
        ]

    def plan(self, actor, sid, pid):
        self.authorize(actor, sid)
        p = self.b.plan(int(pid), public=False)
        if p.get("server_id") not in (None, int(sid)):
            raise TenantBusinessError("plan does not belong to server")
        return p

    def edit_plan(self, actor, sid, pid, changes):
        p = self.plan(actor, sid, pid)
        if p.get("is_dynamic"):
            raise TenantBusinessError("checkout quotes are immutable")
        allowed = {
            "name",
            "traffic_gb",
            "duration_days",
            "price",
            "currency",
            "status",
            "category_id",
            "priority",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("invalid plan edit")
        for key in ("traffic_gb", "duration_days"):
            if key in changes and int(changes[key]) <= 0:
                raise ValueError("invalid plan package")
        for key in ("price", "priority"):
            if key in changes and int(changes[key]) < 0:
                raise ValueError("invalid plan price")
        if "name" in changes and not 1 <= len(str(changes["name"]).strip()) <= 80:
            raise ValueError("invalid plan name")
        if "currency" in changes and changes["currency"] not in {
            "IRR",
            "IRT",
            "USD",
            "USDT",
            "EUR",
        }:
            raise ValueError("invalid plan currency")
        if "status" in changes and changes["status"] not in {
            "active",
            "disabled",
            "archived",
        }:
            raise ValueError("invalid plan status")
        if changes.get("category_id"):
            self.b.plan_category(int(changes["category_id"]), public=False)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_sale_plans SET "
                + ",".join(f"{k}=?" for k in changes)
                + ",updated_at=? WHERE tenant_id=? AND id=?",
                (*changes.values(), iso_utc(utcnow()), self.tenant_id, int(pid)),
            )
        return self.b.plan(int(pid), public=False)

    def domains(self, actor, sid):
        server = self.authorize(actor, sid)
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM tenant_server_domains WHERE tenant_id=? AND server_id=? ORDER BY is_primary DESC,id",
                (self.tenant_id, int(sid)),
            ).fetchall()
        ]
        return rows

    def save_domain(self, actor, sid, *, title, origin, did=None):
        self.authorize(actor, sid)
        clean = url(origin, domain=True)
        if not 1 <= len(str(title).strip()) <= 80:
            raise ValueError("invalid domain title")
        if did is not None:
            self.domain(actor, sid, did)
        with transaction(self.conn):
            if did is None:
                cursor = self.conn.execute(
                    "INSERT INTO tenant_server_domains (tenant_id,server_id,title,origin,is_primary) VALUES (?,?,?,?,?)",
                    (
                        self.tenant_id,
                        int(sid),
                        str(title).strip(),
                        clean,
                        int(not self.domains(actor, sid)),
                    ),
                )
                did = cursor.lastrowid
            else:
                self.conn.execute(
                    "UPDATE tenant_server_domains SET title=?,origin=? WHERE tenant_id=? AND server_id=? AND id=?",
                    (str(title).strip(), clean, self.tenant_id, int(sid), int(did)),
                )
        return self.domain(actor, sid, did)

    def domain(self, actor, sid, did):
        matches = [r for r in self.domains(actor, sid) if r["id"] == int(did)]
        if not matches:
            raise TenantBusinessError("domain does not belong to server")
        return matches[0]

    def select_domain(self, actor, sid, did):
        self.domain(actor, sid, did)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_server_domains SET is_primary=0 WHERE tenant_id=? AND server_id=?",
                (self.tenant_id, int(sid)),
            )
            self.conn.execute(
                "UPDATE tenant_server_domains SET is_primary=1 WHERE tenant_id=? AND server_id=? AND id=?",
                (self.tenant_id, int(sid), int(did)),
            )

    def delete_domain(self, actor, sid, did):
        self.domain(actor, sid, did)
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM tenant_server_domains WHERE tenant_id=? AND server_id=? AND id=?",
                (self.tenant_id, int(sid), int(did)),
            )
            rows = self.domains(actor, sid)
            if rows and not any(r["is_primary"] for r in rows):
                self.conn.execute(
                    "UPDATE tenant_server_domains SET is_primary=1 WHERE tenant_id=? AND id=?",
                    (self.tenant_id, rows[0]["id"]),
                )

    def frozen(self, actor, parent):
        self.authorize(actor, parent)
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT n.*,u.id AS user_id,u.name,u.server_id AS parent_id,s.label AS server_label "
                "FROM tenant_panel_user_nodes n JOIN tenant_panel_users u ON u.id=n.source_user_id AND u.tenant_id=n.tenant_id "
                "JOIN tenant_servers s ON s.id=n.server_id AND s.tenant_id=n.tenant_id "
                "WHERE n.tenant_id=? AND u.server_id=? AND u.state!='deleted' AND (n.frozen_at IS NOT NULL OR n.last_error IS NOT NULL OR n.fail_count>0)",
                (self.tenant_id, int(parent)),
            ).fetchall()
        ]
        for r in self.conn.execute(
            "SELECT n.*,u.id AS user_id,u.name,srv.label AS server_label "
            "FROM tenant_subscription_nodes n JOIN tenant_subscriptions s ON s.id=n.subscription_id AND s.tenant_id=n.tenant_id "
            "JOIN tenant_panel_users u ON u.tenant_id=s.tenant_id AND u.server_id=? "
            "AND (u.external_ref=s.external_ref OR u.external_ref IN (SELECT external_ref FROM tenant_subscription_nodes WHERE tenant_id=s.tenant_id AND subscription_id=s.id AND server_id=?)) "
            "JOIN tenant_servers srv ON srv.id=n.server_id AND srv.tenant_id=n.tenant_id "
            "WHERE n.tenant_id=? AND u.state!='deleted' AND (s.server_id=? OR n.server_id=?) AND (n.frozen_at IS NOT NULL OR n.last_error IS NOT NULL OR n.fail_count>0)",
            (int(parent), int(parent), self.tenant_id, int(parent), int(parent)),
        ).fetchall():
            rows.append(dict(r))
        unique = {(r["user_id"], r["server_id"]): r for r in rows}
        return list(unique.values())

    def clear_frozen(self, actor, parent, uid):
        user = self.user(actor, parent, uid)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_panel_user_nodes SET last_error=NULL,fail_count=0,frozen_at=NULL WHERE tenant_id=? AND source_user_id=?",
                (self.tenant_id, int(uid)),
            )
            if user["subscription_id"]:
                self.conn.execute(
                    "UPDATE tenant_subscription_nodes SET last_error=NULL,fail_count=0,frozen_at=NULL WHERE tenant_id=? AND subscription_id=?",
                    (self.tenant_id, user["subscription_id"]),
                )

    def node_members(self, actor, parent, sid):
        self.authorize(actor, parent)
        # Stored relationships, rather than a currently visible inventory, own
        # replicas. This also covers users removed manually from the primary.
        managed = [
            dict(x)
            for x in self.conn.execute(
                "SELECT n.*,s.external_ref AS source_ref,s.status AS source_status FROM tenant_subscription_nodes n "
                "JOIN tenant_subscriptions s ON s.id=n.subscription_id AND s.tenant_id=n.tenant_id "
                "WHERE n.tenant_id=? AND n.server_id=? AND s.server_id=? AND n.external_ref IS NOT NULL",
                (self.tenant_id, int(sid), int(parent)),
            ).fetchall()
        ]
        native = [
            dict(x)
            for x in self.conn.execute(
                "SELECT n.*,u.external_ref AS source_ref FROM tenant_panel_user_nodes n "
                "JOIN tenant_panel_users u ON u.id=n.source_user_id AND u.tenant_id=n.tenant_id "
                "WHERE n.tenant_id=? AND n.server_id=? AND u.server_id=?",
                (self.tenant_id, int(sid), int(parent)),
            ).fetchall()
        ]
        # Failed managed syncs can have a temporary native failure record.
        managed_refs = {x["external_ref"] for x in managed}
        return managed + [x for x in native if x["external_ref"] not in managed_refs]

    async def renew_user(self, actor, sid, uid, *, gb, days, operation_key):
        row, targets = self.related_targets(actor, sid, uid)
        if row["subscription_id"]:
            return self.b.renew_subscription(
                actor,
                subscription_id=row["subscription_id"],
                traffic_gb=int(gb),
                duration_days=int(days),
                idempotency_key=operation_key,
            )
        request = RenewRequest(
            traffic_bytes=int(gb) * 1024**3,
            duration_days=int(days),
            expires_at=iso_utc(utcnow() + timedelta(days=int(days))),
            idempotency_key=operation_key,
        )
        for target, ref in targets:
            data = await self.call(target, "renew", external_ref=ref, request=request)
            with transaction(self.conn):
                self.save_user(target, asdict(data))
        return await self.live_user(actor, sid, uid)

    async def remove_node(self, actor, parent, nid):
        node = self.node(actor, parent, nid)
        sid = int(node["server_id"])
        inventory = {r["external_ref"]: r for r in await self.refresh_users(actor, sid)}
        for mapping in self.node_members(actor, parent, sid):
            subid = mapping.get("subscription_id")
            if subid:
                self.b._require_completed_rotation(subid)
            data = inventory.get(mapping["external_ref"])
            if data is not None:
                await self.call(
                    sid, "delete_user", external_ref=mapping["external_ref"]
                )
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tenant_panel_users SET state='deleted',active=0 WHERE tenant_id=? AND server_id=? AND external_ref=?",
                    (self.tenant_id, sid, mapping["external_ref"]),
                )
                if subid:
                    carried = (
                        (
                            max(0, int(data["usage_bytes"]))
                            + int(mapping["usage_offset_bytes"] or 0)
                        )
                        if data is not None
                        else max(0, int(mapping["usage_bytes"] or 0))
                    )
                    self.conn.execute(
                        "UPDATE tenant_subscription_nodes SET usage_offset_bytes=usage_offset_bytes+?,usage_bytes=usage_bytes+? WHERE tenant_id=? AND subscription_id=? AND is_primary=1",
                        (carried, carried, self.tenant_id, subid),
                    )
                    self.conn.execute(
                        "UPDATE tenant_subscription_nodes SET external_ref=NULL,status='disabled',usage_bytes=0,usage_offset_bytes=0 WHERE tenant_id=? AND id=?",
                        (self.tenant_id, mapping["id"]),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM tenant_panel_user_nodes WHERE tenant_id=? AND source_user_id=? AND server_id=?",
                        (self.tenant_id, mapping["source_user_id"], sid),
                    )
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM tenant_panel_user_nodes WHERE tenant_id=? AND server_id=? AND source_user_id IN (SELECT id FROM tenant_panel_users WHERE tenant_id=? AND server_id=?)",
                (self.tenant_id, sid, self.tenant_id, int(parent)),
            )
        return self.b.delete_node(actor, node_id=int(nid), parent_server_id=int(parent))

    def discount_active(self, settings, kind):
        until = settings.get(f"discount_{kind}_until")
        return bool(settings.get(f"discount_{kind}_enabled")) and (
            not until or parse_utc(until) > utcnow()
        )

    def discount_percent(self, sid, gb):
        s = self.sales(sid)
        simple = tiered = 0
        if self.discount_active(s, "simple") and s["discount_step_gb"] > 0:
            simple = int(gb) // s["discount_step_gb"] * s["discount_percent_step"]
            if s["discount_percent_max"] > 0:
                simple = min(simple, s["discount_percent_max"])
        if self.discount_active(s, "tiered"):
            for tier in s["discount_tiers"]:
                if int(gb) >= tier["gb"]:
                    tiered = tier["percent"]
        return max(0, min(100, max(simple, tiered, s["discount_percent"])))

    def quote(self, sid, gb, days):
        s = self.sales(sid)
        if (
            s["mode"] not in {"dynamic", "mixed"}
            or int(s["price_gb"]) + int(s["price_day"]) <= 0
        ):
            raise TenantBusinessError("dynamic pricing is not configured")
        if (
            not s["min_gb"] <= int(gb) <= s["max_gb"]
            or not s["min_days"] <= int(days) <= s["max_days"]
        ):
            raise ValueError("package outside configured bounds")
        price = (
            (int(gb) * s["price_gb"] + int(days) * s["price_day"])
            * (100 - self.discount_percent(sid, gb))
            // 100
        )
        return price, s["currency"]

    def dynamic_plan(self, actor, sid, gb, days):
        self.b._customer(actor)
        self.b._purchase_server(int(sid))
        price, currency = self.quote(sid, gb, days)
        # A quote becomes an immutable ordinary plan/order snapshot at checkout.
        name = f"پلن پویا سرور {sid}: {gb}GB · {days} روز · {price} {currency}"
        row = self.conn.execute(
            "SELECT id FROM tenant_sale_plans WHERE tenant_id=? AND server_id=? AND name=? AND status='active'",
            (self.tenant_id, int(sid), name),
        ).fetchone()
        if row:
            return self.b.plan(row["id"])
        plan = self.b.add_plan(
            self.b.owner_telegram_id,
            name=name,
            traffic_gb=int(gb),
            duration_days=int(days),
            price=price,
            currency=currency,
            server_id=int(sid),
        )
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_sale_plans SET is_dynamic=1 WHERE tenant_id=? AND id=?",
                (self.tenant_id, plan["id"]),
            )
        return self.b.plan(plan["id"])

    async def set_node_enabled(self, actor, parent, nid, enabled):
        node = self.node(actor, parent, nid)
        sid = int(node["server_id"])
        source = {r["external_ref"]: r for r in await self.refresh_users(actor, parent)}
        for mapping in self.node_members(actor, parent, sid):
            subid = mapping.get("subscription_id")
            if subid:
                self.b._require_completed_rotation(subid)
            origin = source.get(mapping["source_ref"])
            active = bool(enabled) and bool(origin) and user_status(origin) == "active"
            if subid:
                sub = self.b._admin_subscription(actor, subid)
                active = (
                    active
                    and sub["status"] == "active"
                    and not self.b._subscription_is_due(sub)
                )
            data = await self.call(
                sid, "set_enabled", external_ref=mapping["external_ref"], enabled=active
            )
            with transaction(self.conn):
                self.save_user(sid, asdict(data))
                if subid:
                    self.conn.execute(
                        "UPDATE tenant_subscription_nodes SET status=?,usage_bytes=?,updated_at=? WHERE tenant_id=? AND subscription_id=? AND server_id=?",
                        (
                            "active" if data.active else "disabled",
                            max(0, int(data.usage_bytes))
                            + int(mapping["usage_offset_bytes"] or 0),
                            iso_utc(utcnow()),
                            self.tenant_id,
                            subid,
                            sid,
                        ),
                    )
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE tenant_nodes SET status=?,updated_at=? WHERE tenant_id=? AND id=?",
                (
                    "active" if enabled else "disabled",
                    iso_utc(utcnow()),
                    self.tenant_id,
                    int(nid),
                ),
            )
