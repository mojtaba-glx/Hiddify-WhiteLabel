"""Tenant-scoped bank-SMS webhook (Hiddify-SellBot parity).

The Android SMS-verifier app POSTs bank transfer notifications here so
pending card-to-card receipts can be approved without the admin being
online. Every tenant has its own secret and route::

    POST /payment/sms/{tenant_public_id}

Auth: ``X-WhiteLabel-Sms-Secret`` header (``X-SellBot-Sms-Secret`` accepted
too) compared in constant time against the tenant's stored secret. A valid
secret only authorizes its own tenant's route — never another tenant.

Payload (JSON)::

    {
      "event_id": "unique-per-sms",
      "amount": 1302630, "currency": "rial",
      "reference": "...", "sender": "20004861",
      "card_last4": "1234", "body": "...",
      "received_at": 1699999999000, "device_time": 1699999999000,
      "test": false
    }

Matching mirrors SellBot: amount candidates in toman, optional payer
last4, a bounded time window, ambiguity rejection (409) and anti-replay
(no double approval, late SMS attaches to manual approvals instead of
approving a stranger's receipt). Approval reuses the exact same
``review_receipt(approve=True)`` path as the AdminBot button, so
provisioning, referral credit and audit behave identically.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Any

from Database.connection import connect as open_database
from Database.repositories import BotRepository
from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessError, TenantBusinessService

SMS_SECRET_HEADERS = ("X-WhiteLabel-Sms-Secret", "X-SellBot-Sms-Secret")
MAX_BODY_BYTES = 64 * 1024


def normalize_sms_currency(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if text in {"rial", "irr", "ریال", "ريال"}:
        return "rial"
    if text in {"toman", "تومان"}:
        return "toman"
    return "unknown"


def sms_amount_candidates_toman(amount_raw: int, currency_raw: str) -> list[int]:
    try:
        amount = int(amount_raw or 0)
    except (TypeError, ValueError):
        return []
    if amount <= 0:
        return []
    currency = normalize_sms_currency(currency_raw)
    if currency == "rial":
        candidates = [int(round(amount / 10))]
    elif currency == "toman":
        candidates = [amount]
    else:
        candidates = [amount]
    return [item for item in candidates if item > 0]


def mask_secret(secret: object) -> str:
    return TenantBusinessService.mask_sms_secret(secret)


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value or "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return int(default)


def _event_datetime_ms(value_ms: int):
    from datetime import datetime, timezone

    try:
        moment = int(value_ms or 0)
    except (TypeError, ValueError):
        return None
    if moment <= 0:
        return None
    try:
        return datetime.fromtimestamp(moment / 1000, tz=timezone.utc).replace(
            tzinfo=None
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _telegram_api(token: str, method: str, fields: dict[str, Any]) -> None:
    data = urllib.parse.urlencode(
        {str(key): str(value) for key, value in fields.items()}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read(512)


def _notify_admin_report(
    conn: sqlite3.Connection,
    business: TenantBusinessService,
    *,
    receipt: dict[str, Any],
    customer: dict[str, Any],
    amount_toman: int,
    sender: str,
    reference: str,
    amount_raw: int,
    currency_raw: str,
) -> None:
    try:
        from Shared.crypto import fingerprint_token

        token = ""
        admin_row = BotRepository(conn).get_by_tenant_role(
            business.tenant_id, "admin"
        )
        if admin_row is None or admin_row.get("status") != "active":
            return
        if business.secret_cipher is None:
            return
        plain = business.secret_cipher.decrypt(str(admin_row["encrypted_token"]))
        if fingerprint_token(plain) != str(admin_row["token_fingerprint"]):
            return
        token = plain
        display = (
            str(customer.get("display_name") or "").strip()
            or str(customer.get("username") or "").strip()
            or str(customer.get("telegram_user_id") or "-")
        )
        has_receipt = bool(
            str(receipt.get("telegram_file_id") or "").strip()
            or str(receipt.get("reference") or "").strip()
        )
        text = (
            "✅ پرداخت کارت‌به‌کارت با SMS تایید شد.\n"
            f"🧾 سفارش: #{int(receipt.get('order_id') or 0)}\n"
            f"👤 کاربر: {display}\n"
            f"💰 مبلغ: {int(amount_toman):,} تومان\n"
            f"📨 سرشماره SMS: {sender or '-'}\n"
            f"🏦 مبلغ خام SMS: {int(amount_raw):,} {currency_raw or ''}\n"
            f"🔖 پیگیری SMS: {reference or '-'}\n"
            f"🖼 رسید کاربر: {'پیوست شد' if has_receipt else 'در دسترس نیست'}"
        )
        _telegram_api(
            token,
            "sendMessage",
            {"chat_id": int(business.owner_telegram_id), "text": text},
        )
    except Exception:
        pass
    finally:
        token = ""


def _notify_user_approved(
    conn: sqlite3.Connection,
    business: TenantBusinessService,
    *,
    customer: dict[str, Any],
    amount_toman: int,
) -> None:
    try:
        from Shared.crypto import fingerprint_token

        token = ""
        user_row = BotRepository(conn).get_by_tenant_role(
            business.tenant_id, "user"
        )
        if user_row is None or user_row.get("status") != "active":
            return
        if business.secret_cipher is None:
            return
        plain = business.secret_cipher.decrypt(str(user_row["encrypted_token"]))
        if fingerprint_token(plain) != str(user_row["token_fingerprint"]):
            return
        token = plain
        chat_id = int(customer.get("telegram_user_id") or 0)
        if chat_id <= 0:
            return
        _telegram_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "✅ پرداخت شما به‌صورت خودکار با SMS بانک تایید شد.\n"
                    f"💰 مبلغ تاییدشده: {int(amount_toman):,} تومان"
                ),
            },
        )
    except Exception:
        pass
    finally:
        token = ""


def _load_business(
    conn: sqlite3.Connection, tenant_id: int, cipher: Any
) -> TenantBusinessService | None:
    row = conn.execute(
        "SELECT id, status, owner_telegram_id FROM tenants WHERE id=?",
        (int(tenant_id),),
    ).fetchone()
    if row is None or str(row["status"]) != "active":
        return None
    try:
        return TenantBusinessService(
            conn,
            tenant_id=int(tenant_id),
            owner_telegram_id=int(row["owner_telegram_id"]),
            secret_cipher=cipher,
        )
    except ValueError:
        return None


def handle_sms_post(
    db_path: str,
    tenant_public_id: str,
    headers: dict[str, str],
    body: bytes,
    *,
    cipher: Any,
) -> tuple[int, dict[str, Any]]:
    """Process one SMS webhook POST. Returns (http_status, json_payload)."""
    conn = open_database(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM tenants WHERE public_id=?",
            (str(tenant_public_id or "").strip(),),
        ).fetchone()
        if row is None:
            return 404, {"ok": False, "error": "unknown_tenant"}
        business = _load_business(conn, int(row["id"]), cipher)
        if business is None:
            return 403, {"ok": False, "error": "tenant_disabled"}

        settings_row = conn.execute(
            "SELECT enabled FROM tenant_sms_settings WHERE tenant_id=?",
            (business.tenant_id,),
        ).fetchone()
        if settings_row is None or not bool(settings_row["enabled"]):
            return 403, {"ok": False, "error": "sms_webhook_disabled"}

        provided = ""
        lowered = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        for name in SMS_SECRET_HEADERS:
            if lowered.get(name.lower(), "").strip():
                provided = lowered[name.lower()].strip()
                break
        if not business.sms_secret_matches(provided):
            return 401, {"ok": False, "error": "invalid_secret"}

        if not body or len(body) > MAX_BODY_BYTES:
            return 400, {"ok": False, "error": "invalid_body"}
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="ignore"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "invalid_json"}
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "invalid_payload"}

        event_id = str(payload.get("event_id") or "").strip()
        if (
            not event_id
            or len(event_id) > 128
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in event_id)
        ):
            return 400, {"ok": False, "error": "event_id_required"}
        amount_raw = _to_int(payload.get("amount"), 0)
        currency_raw = normalize_sms_currency(payload.get("currency"))
        reference = str(payload.get("reference") or "").strip()
        sender = str(payload.get("sender") or "").strip()
        card_last4 = re.sub(r"\D", "", str(payload.get("card_last4") or ""))[-4:]
        sms_body = str(payload.get("body") or "")
        received_at_ms = _to_int(payload.get("received_at"), 0)
        device_time_ms = _to_int(payload.get("device_time"), 0)
        sms_time_ms = int(received_at_ms or device_time_ms or 0)

        if bool(payload.get("test")):
            inserted, existing = business.record_sms_event(
                {
                    "event_id": event_id,
                    "sender": sender,
                    "amount_raw": amount_raw,
                    "currency_raw": currency_raw,
                    "amount_toman": 0,
                    "reference": reference,
                    "card_last4": card_last4,
                    "body": sms_body,
                    "status": "test_received",
                    "message": "test webhook received",
                    "received_at": received_at_ms,
                    "device_time": device_time_ms,
                }
            )
            return 200, {
                "ok": True,
                "test": True,
                "duplicate": not inserted,
            }

        candidates = sms_amount_candidates_toman(amount_raw, currency_raw)
        if not candidates:
            business.record_sms_event(
                {
                    "event_id": event_id,
                    "sender": sender,
                    "amount_raw": amount_raw,
                    "currency_raw": currency_raw,
                    "amount_toman": 0,
                    "reference": reference,
                    "card_last4": card_last4,
                    "body": sms_body,
                    "status": "invalid_amount",
                    "message": "amount not found or invalid",
                    "received_at": received_at_ms,
                    "device_time": device_time_ms,
                }
            )
            return 422, {"ok": False, "error": "invalid_amount"}

        inserted, existing = business.record_sms_event(
            {
                "event_id": event_id,
                "sender": sender,
                "amount_raw": amount_raw,
                "currency_raw": currency_raw,
                "amount_toman": int(candidates[0]),
                "reference": reference,
                "card_last4": card_last4,
                "body": sms_body,
                "status": "received",
                "message": "received",
                "received_at": received_at_ms,
                "device_time": device_time_ms,
            }
        )
        if not inserted:
            status = str((existing or {}).get("status") or "")
            matched = (existing or {}).get("matched_receipt_id")
            return 200, {
                "ok": True,
                "duplicate": True,
                "status": status,
                "matched_receipt_id": matched,
            }

        return _try_approve_event(
            conn,
            business,
            event_id=event_id,
            amount_raw=amount_raw,
            currency_raw=currency_raw,
            candidates=candidates,
            reference=reference,
            sender=sender,
            card_last4=card_last4,
            sms_time_ms=sms_time_ms,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _try_approve_event(
    conn: sqlite3.Connection,
    business: TenantBusinessService,
    *,
    event_id: str,
    amount_raw: int,
    currency_raw: str,
    candidates: list[int],
    reference: str,
    sender: str,
    card_last4: str,
    sms_time_ms: int,
) -> tuple[int, dict[str, Any]]:
    settings = business.sms_status_admin(business.owner_telegram_id)
    max_age = int(
        settings.get("max_pending_age_minutes")
        or TenantBusinessService.SMS_DEFAULT_MAX_PENDING_AGE_MINUTES
    )

    # Anti-cross-user-replay: a late SMS for a manually approved receipt
    # attaches to it instead of approving a stranger's pending receipt.
    for amount_toman in candidates:
        for approved in business.recently_approved_card_receipts_for_sms(
            int(amount_toman),
            max_age_minutes=TenantBusinessService.SMS_MANUAL_ATTACH_WINDOW_MINUTES,
        ):
            business.update_sms_event(
                event_id,
                status="attached_manual",
                message="bank SMS attached to admin-approved receipt",
                amount_toman=int(amount_toman),
                matched_receipt_id=int(approved["id"]),
            )
            return 200, {
                "ok": True,
                "matched": True,
                "status": "attached_manual",
                "receipt_id": int(approved["id"]),
                "amount_toman": int(amount_toman),
            }

    matches: list[dict[str, Any]] = []
    matched_amount = 0
    for amount_toman in candidates:
        matches = business.find_pending_card_receipts_for_sms(
            int(amount_toman),
            card_last4=card_last4,
            max_age_minutes=max_age,
            sms_time_ms=sms_time_ms,
        )
        if matches:
            matched_amount = int(amount_toman)
            break
    if not matches:
        business.update_sms_event(
            event_id,
            status="no_pending_match",
            message=f"no pending card receipt for candidates={candidates}",
            amount_toman=int(candidates[0]),
        )
        return 202, {
            "ok": True,
            "matched": False,
            "status": "no_pending_match",
            "amount_candidates_toman": candidates,
        }
    if len(matches) > 1:
        business.update_sms_event(
            event_id,
            status="ambiguous",
            message=f"multiple pending card receipts matched amount={matched_amount}",
            amount_toman=matched_amount,
        )
        return 409, {
            "ok": False,
            "matched": False,
            "error": "ambiguous_pending_payments",
            "amount_toman": matched_amount,
            "count": len(matches),
        }

    receipt = matches[0]
    try:
        result = business.approve_receipt_by_sms(
            int(receipt["id"]),
            event_id=event_id,
            sender=sender,
        )
    except (TenantBusinessError, sqlite3.IntegrityError, ValueError) as exc:
        business.update_sms_event(
            event_id,
            status="approve_failed",
            message=str(exc)[:200],
            amount_toman=matched_amount,
        )
        return 500, {"ok": False, "error": "approve_failed"}
    business.update_sms_event(
        event_id,
        status="approved",
        message="receipt approved by bank SMS",
        amount_toman=matched_amount,
        matched_receipt_id=int(receipt["id"]),
    )
    customer = dict(
        conn.execute(
            "SELECT display_name, username, telegram_user_id FROM tenant_customers"
            " WHERE id=? AND tenant_id=?",
            (int(result.get("customer_id") or 0), business.tenant_id),
        ).fetchone()
        or {}
    )
    _notify_admin_report(
        conn,
        business,
        receipt=dict(receipt),
        customer=customer,
        amount_toman=matched_amount,
        sender=sender,
        reference=reference,
        amount_raw=amount_raw,
        currency_raw=currency_raw,
    )
    _notify_user_approved(
        conn, business, customer=customer, amount_toman=matched_amount
    )
    return 200, {
        "ok": True,
        "matched": True,
        "status": "approved",
        "receipt_id": int(receipt["id"]),
        "order_id": int(result.get("order_id") or 0),
        "amount_toman": matched_amount,
    }


def sms_test_event_id() -> str:
    from uuid import uuid4 as _uuid4

    return f"sms-test-{_uuid4().hex[:16]}"


def current_utc_iso() -> str:
    return iso_utc(utcnow())
