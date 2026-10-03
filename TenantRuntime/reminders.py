"""Durable tenant subscription reminders delivered through each tenant UserBot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from Shared.crypto import TokenCipher, fingerprint_token
from Shared.timeutils import ensure_utc, iso_utc, parse_utc, utcnow

_GIB = 1024**3


@dataclass(frozen=True)
class ReminderDelivery:
    event_key: str
    tenant_id: int
    subscription_id: int
    chat_id: int
    plan_name: str
    event_type: str
    stage_value: int
    expires_at: str
    usage_bytes: int
    traffic_bytes: int
    bot_token: str = field(repr=False)


@dataclass
class ReminderQueueReport:
    enqueued: int = 0
    recovered: int = 0
    requeued: int = 0
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    abandoned: int = 0
    skipped: int = 0
    errors: int = 0


class ReminderSender(Protocol):
    async def send(self, delivery: ReminderDelivery) -> None: ...


class TelegramReminderSender:
    """Send from the tenant's own UserBot; no MasterBot credentials are used."""

    async def send(self, delivery: ReminderDelivery) -> None:
        title = str(delivery.plan_name or "اشتراک شما").strip() or "اشتراک شما"
        if delivery.event_type == "days":
            text = (
                "🚨 یادآوری تمدید اشتراک\n"
                f"🔹 اشتراک: «{title}»\n"
                f"📅 زمان باقی‌مانده: {delivery.stage_value} روز\n"
                "برای جلوگیری از قطع سرویس، اشتراک را تمدید کنید."
            )
        elif delivery.event_type == "usage":
            text = (
                "🚨 یادآوری تمدید اشتراک\n"
                f"🔹 اشتراک: «{title}»\n"
                f"🚥 حجم باقی‌مانده: {delivery.stage_value} گیگ\n"
                "برای جلوگیری از قطع سرویس، اشتراک را تمدید کنید."
            )
        else:
            time_done = parse_utc(delivery.expires_at) <= utcnow()
            usage_done = (
                int(delivery.traffic_bytes) > 0
                and int(delivery.usage_bytes) >= int(delivery.traffic_bytes)
            )
            if time_done and usage_done:
                detail = "حجم و مدت اشتراک شما به اتمام رسیده است."
            elif usage_done:
                detail = "حجم اشتراک شما به اتمام رسیده است."
            else:
                detail = "مدت اشتراک شما به اتمام رسیده است."
            text = (
                "⚠️ اشتراک شما منقضی شد\n"
                f"🔹 اشتراک: «{title}»\n"
                f"{detail}\n"
                "برای فعال‌سازی دوباره، ابتدا اشتراک را تمدید کنید."
            )
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "♻️ تمدید اشتراک",
                    callback_data=f"shop:renew:{int(delivery.subscription_id)}",
                )
            ]]
        )
        async with Bot(token=delivery.bot_token) as bot:
            await bot.send_message(
                chat_id=int(delivery.chat_id),
                text=text,
                reply_markup=keyboard,
            )


def reminder_period_key(subscription: dict) -> str:
    material = (
        f"{int(subscription['id'])}|{subscription.get('expires_at') or ''}|"
        f"{int(subscription.get('traffic_bytes') or 0)}|"
        f"{int(subscription.get('plan_id') or 0)}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _event_key(
    *, tenant_id: int, subscription_id: int, event_type: str,
    stage_value: int, period_key: str
) -> str:
    return (
        f"sub:{int(tenant_id)}:{int(subscription_id)}:"
        f"{event_type}:{int(stage_value)}:{period_key}"
    )


def _days_bucket(expires_at: str, now: datetime) -> int:
    seconds = (parse_utc(str(expires_at)) - ensure_utc(now)).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int(math.ceil(seconds / 86400.0)))


def _usage_bucket(traffic_bytes: int, usage_bytes: int) -> int:
    remaining = int(traffic_bytes) - int(usage_bytes)
    if remaining <= 0:
        return 0
    return max(1, int(math.ceil(remaining / float(_GIB))))


def enqueue_due_reminders(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    days_threshold: int,
    remaining_gb_threshold: int,
    now: datetime | None = None,
) -> int:
    """Create period-scoped reminders once; renewal naturally creates fresh keys."""
    moment = ensure_utc(now) if now is not None else utcnow()
    now_iso = iso_utc(moment)
    rows = conn.execute(
        "SELECT s.*, c.telegram_user_id, p.name AS plan_name "
        "FROM tenant_subscriptions s "
        "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
        "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
        "WHERE s.tenant_id=? AND s.status IN ('active','disabled','expired') "
        "ORDER BY s.id",
        (int(tenant_id),),
    ).fetchall()
    inserted = 0
    for raw in rows:
        sub = dict(raw)
        period = reminder_period_key(sub)
        events: list[tuple[str, int]] = []
        if str(sub["status"]) == "expired":
            events.append(("expired", 0))
        else:
            try:
                days = _days_bucket(str(sub["expires_at"]), moment)
            except Exception:
                days = 0
            if 0 < days <= max(1, int(days_threshold)):
                events.append(("days", days))
            usage_bucket = _usage_bucket(
                int(sub.get("traffic_bytes") or 0),
                int(sub.get("usage_bytes") or 0),
            )
            if 0 < usage_bucket <= max(1, int(remaining_gb_threshold)):
                events.append(("usage", usage_bucket))

        for event_type, stage in events:
            key = _event_key(
                tenant_id=int(tenant_id),
                subscription_id=int(sub["id"]),
                event_type=event_type,
                stage_value=stage,
                period_key=period,
            )
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO tenant_subscription_notifications "
                "(tenant_id, subscription_id, customer_id, event_type, "
                "stage_value, period_key, event_key, status, scheduled_at, "
                "retry_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?)",
                (
                    int(tenant_id),
                    int(sub["id"]),
                    int(sub["customer_id"]),
                    event_type,
                    int(stage),
                    period,
                    key,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            inserted += int(conn.total_changes > before)
    return inserted


def reconcile_reminder_queue_settings(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    enabled: bool,
    days_threshold: int,
    remaining_gb_threshold: int,
    now: datetime | None = None,
) -> int:
    moment = ensure_utc(now) if now is not None else utcnow()
    now_iso = iso_utc(moment)
    if bool(enabled):
        changed = conn.execute(
            "UPDATE tenant_subscription_notifications "
            "SET status='skipped', locked_at=NULL, next_attempt_at=NULL, updated_at=? "
            "WHERE tenant_id=? AND status IN ('pending','failed','processing') "
            "AND ((event_type='days' AND stage_value>?) "
            "OR (event_type='usage' AND stage_value>?))",
            (
                now_iso,
                int(tenant_id),
                max(1, int(days_threshold)),
                max(1, int(remaining_gb_threshold)),
            ),
        )
    else:
        changed = conn.execute(
            "UPDATE tenant_subscription_notifications "
            "SET status='skipped', locked_at=NULL, next_attempt_at=NULL, updated_at=? "
            "WHERE tenant_id=? AND status IN ('pending','failed','processing')",
            (now_iso, int(tenant_id)),
        )
    return max(0, int(changed.rowcount))


def _mark_skipped(conn: sqlite3.Connection, key: str, now_iso: str) -> None:
    conn.execute(
        "UPDATE tenant_subscription_notifications "
        "SET status='skipped', locked_at=NULL, next_attempt_at=NULL, updated_at=? "
        "WHERE event_key=? AND status='processing'",
        (now_iso, str(key)),
    )


def _mark_failed(
    conn: sqlite3.Connection,
    row: dict,
    *,
    now: datetime,
    max_retries: int,
    retry_base_seconds: int,
) -> tuple[bool, bool]:
    attempt = int(row.get("retry_count") or 0) + 1
    next_attempt = None
    retried = False
    abandoned = False
    if attempt < int(max_retries):
        delay = int(retry_base_seconds) * (2 ** min(attempt - 1, 6))
        next_attempt = iso_utc(ensure_utc(now) + timedelta(seconds=delay))
        retried = True
    else:
        abandoned = True
    conn.execute(
        "UPDATE tenant_subscription_notifications "
        "SET status='failed', retry_count=retry_count+1, locked_at=NULL, "
        "next_attempt_at=?, updated_at=? "
        "WHERE event_key=? AND status='processing'",
        (next_attempt, iso_utc(now), str(row["event_key"])),
    )
    return retried, abandoned


def _build_delivery(
    conn: sqlite3.Connection,
    *,
    row: dict,
    cipher: TokenCipher,
    now: datetime,
) -> ReminderDelivery | None:
    current_row = conn.execute(
        "SELECT s.*, c.telegram_user_id, c.status AS customer_status, "
        "p.name AS plan_name, t.status AS tenant_status "
        "FROM tenant_subscriptions s "
        "JOIN tenant_customers c ON c.id=s.customer_id AND c.tenant_id=s.tenant_id "
        "JOIN tenant_sale_plans p ON p.id=s.plan_id AND p.tenant_id=s.tenant_id "
        "JOIN tenants t ON t.id=s.tenant_id "
        "WHERE s.id=? AND s.tenant_id=? AND s.customer_id=?",
        (
            int(row["subscription_id"]),
            int(row["tenant_id"]),
            int(row["customer_id"]),
        ),
    ).fetchone()
    if current_row is None:
        return None
    current = dict(current_row)
    if (
        str(current.get("tenant_status") or "") != "active"
        or str(current.get("customer_status") or "") != "active"
    ):
        return None
    if reminder_period_key(current) != str(row["period_key"]):
        return None

    event_type = str(row["event_type"])
    stage = int(row["stage_value"] or 0)
    status = str(current["status"])
    if event_type == "expired":
        if status != "expired":
            return None
    elif status not in ("active", "disabled"):
        return None
    elif event_type == "days":
        if _days_bucket(str(current["expires_at"]), now) != stage:
            return None
    elif event_type == "usage":
        if _usage_bucket(
            int(current["traffic_bytes"]), int(current["usage_bytes"])
        ) != stage:
            return None
    else:
        return None

    bot_row = conn.execute(
        "SELECT encrypted_token, token_fingerprint FROM tenant_bots "
        "WHERE tenant_id=? AND role='user' AND status='active' LIMIT 1",
        (int(row["tenant_id"]),),
    ).fetchone()
    if bot_row is None:
        raise RuntimeError("tenant UserBot is unavailable")
    token = cipher.decrypt(str(bot_row["encrypted_token"]))
    if not hmac.compare_digest(
        fingerprint_token(token), str(bot_row["token_fingerprint"])
    ):
        raise RuntimeError("tenant UserBot credential integrity failed")
    try:
        return ReminderDelivery(
            event_key=str(row["event_key"]),
            tenant_id=int(row["tenant_id"]),
            subscription_id=int(row["subscription_id"]),
            chat_id=int(current["telegram_user_id"]),
            plan_name=str(current["plan_name"]),
            event_type=event_type,
            stage_value=stage,
            expires_at=str(current["expires_at"]),
            usage_bytes=int(current["usage_bytes"]),
            traffic_bytes=int(current["traffic_bytes"]),
            bot_token=token,
        )
    finally:
        # The immutable delivery owns its string copy; this only drops the local name.
        token = ""


def claim_due_deliveries(
    conn: sqlite3.Connection,
    *,
    cipher: TokenCipher,
    shard_count: int,
    shard_index: int,
    lease_seconds: int,
    max_retries: int,
    retry_base_seconds: int,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[list[ReminderDelivery], ReminderQueueReport]:
    moment = ensure_utc(now) if now is not None else utcnow()
    now_iso = iso_utc(moment)
    report = ReminderQueueReport()

    stale_before = iso_utc(moment - timedelta(seconds=max(10, int(lease_seconds))))
    stale = conn.execute(
        "UPDATE tenant_subscription_notifications "
        "SET status='failed', locked_at=NULL, next_attempt_at=?, "
        "retry_count=retry_count+1, updated_at=? "
        "WHERE status='processing' AND locked_at IS NOT NULL AND locked_at<=? "
        "AND (tenant_id % ?) = ?",
        (now_iso, now_iso, stale_before, int(shard_count), int(shard_index)),
    )
    report.recovered = max(0, int(stale.rowcount))

    failed = conn.execute(
        "SELECT event_key FROM tenant_subscription_notifications "
        "WHERE status='failed' AND next_attempt_at IS NOT NULL "
        "AND next_attempt_at<=? AND (tenant_id % ?) = ? "
        "ORDER BY next_attempt_at LIMIT ?",
        (now_iso, int(shard_count), int(shard_index), max(1, min(int(limit), 500))),
    ).fetchall()
    for item in failed:
        changed = conn.execute(
            "UPDATE tenant_subscription_notifications "
            "SET status='pending', scheduled_at=?, next_attempt_at=NULL, "
            "locked_at=NULL, updated_at=? "
            "WHERE event_key=? AND status='failed' "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at<=?",
            (now_iso, now_iso, str(item["event_key"]), now_iso),
        )
        report.requeued += max(0, int(changed.rowcount))

    pending = conn.execute(
        "SELECT * FROM tenant_subscription_notifications "
        "WHERE status='pending' AND scheduled_at<=? AND (tenant_id % ?) = ? "
        "ORDER BY scheduled_at, id LIMIT ?",
        (now_iso, int(shard_count), int(shard_index), max(1, min(int(limit), 500))),
    ).fetchall()
    deliveries: list[ReminderDelivery] = []
    for item in pending:
        row = dict(item)
        claimed = conn.execute(
            "UPDATE tenant_subscription_notifications "
            "SET status='processing', locked_at=?, updated_at=? "
            "WHERE event_key=? AND status='pending' AND scheduled_at<=?",
            (now_iso, now_iso, str(row["event_key"]), now_iso),
        )
        if claimed.rowcount != 1:
            continue
        report.claimed += 1
        fresh = conn.execute(
            "SELECT * FROM tenant_subscription_notifications WHERE event_key=?",
            (str(row["event_key"]),),
        ).fetchone()
        if fresh is None:
            continue
        row = dict(fresh)
        if int(row["retry_count"] or 0) >= int(max_retries):
            conn.execute(
                "UPDATE tenant_subscription_notifications "
                "SET status='failed', locked_at=NULL, next_attempt_at=NULL, updated_at=? "
                "WHERE event_key=? AND status='processing'",
                (now_iso, str(row["event_key"])),
            )
            report.abandoned += 1
            continue
        try:
            delivery = _build_delivery(conn, row=row, cipher=cipher, now=moment)
        except Exception:
            retried, abandoned = _mark_failed(
                conn,
                row,
                now=moment,
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
            )
            report.retried += int(retried)
            report.abandoned += int(abandoned)
            continue
        if delivery is None:
            _mark_skipped(conn, str(row["event_key"]), now_iso)
            report.skipped += 1
            continue
        deliveries.append(delivery)
    return deliveries, report


def finish_delivery(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    success: bool,
    max_retries: int,
    retry_base_seconds: int,
    now: datetime | None = None,
) -> ReminderQueueReport:
    moment = ensure_utc(now) if now is not None else utcnow()
    now_iso = iso_utc(moment)
    report = ReminderQueueReport()
    row = conn.execute(
        "SELECT * FROM tenant_subscription_notifications WHERE event_key=?",
        (str(event_key),),
    ).fetchone()
    if row is None or str(row["status"]) != "processing":
        report.errors = 1
        return report
    if success:
        changed = conn.execute(
            "UPDATE tenant_subscription_notifications "
            "SET status='sent', sent_at=?, locked_at=NULL, next_attempt_at=NULL, "
            "updated_at=? WHERE event_key=? AND status='processing'",
            (now_iso, now_iso, str(event_key)),
        )
        report.sent = int(changed.rowcount == 1)
        report.errors = int(changed.rowcount != 1)
        return report
    retried, abandoned = _mark_failed(
        conn,
        dict(row),
        now=moment,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    report.retried = int(retried)
    report.abandoned = int(abandoned)
    return report
