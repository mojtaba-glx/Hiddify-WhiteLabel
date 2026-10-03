"""Stable v1.0.0 release-gate integration and callback coverage tests.

These tests intentionally cross Phase boundaries.  Individual feature suites
remain the detailed specification; this file is the final release smoke gate
that proves those boundaries still compose into one Tenant runtime.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.backup import (
    BACKUP_FORMAT,
    create_tenant_backup,
    restore_tenant_backup,
)
from TenantRuntime.business import TenantBusinessError, TenantBusinessService
from Tests.test_multinode_smart_subscription import _setup as multinode_setup


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _button_routes(source: str) -> set[str]:
    """Collect literal/dynamic callback prefixes created by Inline buttons.

    Only direct string/f-string callback_data values are included.  Variable
    callback expressions are covered by their owning feature tests instead.
    """
    found: set[str] = set()
    pattern = re.compile(
        r"callback_data\s*=\s*f?([\"'])(.*?)(?<!\\)\1",
        re.DOTALL,
    )
    for match in pattern.finditer(source):
        value = match.group(2).strip()
        if not value or "\n" in value:
            continue
        # Dynamic callbacks are routed by their stable prefix.
        if "{" in value:
            value = value.split("{", 1)[0]
        if value:
            found.add(value)
    return found


def _handled_routes(source: str) -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    prefixes: set[str] = set()

    for match in re.finditer(
        r"\bdata\s*==\s*([\"'])(.*?)(?<!\\)\1",
        source,
    ):
        exact.add(match.group(2))

    for match in re.finditer(
        r"\bdata\.startswith\(\s*([\"'])(.*?)(?<!\\)\1\s*\)",
        source,
    ):
        prefixes.add(match.group(2))

    # startswith(("a:", "b:")) tuple form.
    for match in re.finditer(
        r"\bdata\.startswith\(\s*\((.*?)\)\s*\)",
        source,
        re.DOTALL,
    ):
        block = match.group(1)
        for item in re.finditer(r"([\"'])(.*?)(?<!\\)\1", block):
            prefixes.add(item.group(2))

    # A few handlers use membership sets/tuples for fixed callbacks.
    for match in re.finditer(
        r"\bdata\s+in\s+[\{\(](.*?)[\}\)]",
        source,
        re.DOTALL,
    ):
        block = match.group(1)
        for item in re.finditer(r"([\"'])(.*?)(?<!\\)\1", block):
            exact.add(item.group(2))

    return exact, prefixes


def _route_is_handled(
    route: str,
    *,
    exact: set[str],
    prefixes: set[str],
) -> bool:
    if route in exact:
        return True
    return any(route.startswith(prefix) for prefix in prefixes if prefix)


def test_every_direct_inline_callback_has_a_registered_route() -> None:
    """Fail Stable when a visible inline button has no callback branch."""
    admin_handler = _source("TenantRuntime/AdminBot/handlers.py")
    admin_management = _source("TenantRuntime/AdminBot/userbot_management.py")
    admin_broadcast = _source("TenantRuntime/AdminBot/broadcast_channel.py")
    user_handler = _source("TenantRuntime/UserBot/handlers.py")

    admin_buttons = (
        _button_routes(admin_handler)
        | _button_routes(admin_management)
        | _button_routes(admin_broadcast)
    )
    user_buttons = _button_routes(user_handler)

    admin_exact_1, admin_prefix_1 = _handled_routes(admin_handler)
    admin_exact_2, admin_prefix_2 = _handled_routes(admin_management)
    admin_exact_3, admin_prefix_3 = _handled_routes(admin_broadcast)
    admin_exact = admin_exact_1 | admin_exact_2 | admin_exact_3
    admin_prefix = admin_prefix_1 | admin_prefix_2 | admin_prefix_3

    user_exact, user_prefix = _handled_routes(user_handler)

    missing_admin = sorted(
        route
        for route in admin_buttons
        if not _route_is_handled(
            route,
            exact=admin_exact,
            prefixes=admin_prefix,
        )
    )
    missing_user = sorted(
        route
        for route in user_buttons
        if not _route_is_handled(
            route,
            exact=user_exact,
            prefixes=user_prefix,
        )
    )

    assert missing_admin == [], (
        "AdminBot inline callbacks without a handler route: "
        + ", ".join(missing_admin)
    )
    assert missing_user == [], (
        "UserBot inline callbacks without a handler route: "
        + ", ".join(missing_user)
    )


def test_adminbot_userbot_runtime_contract_is_still_two_role_and_tenant_scoped(
    conn, factories, cipher
) -> None:
    owner = 91001
    tenant = factories.tenant(owner_telegram_id=owner)
    factories.bot(int(tenant["id"]), "admin", cipher)
    factories.bot(int(tenant["id"]), "user", cipher)

    rows = conn.execute(
        "SELECT role,status FROM tenant_bots WHERE tenant_id=? ORDER BY role",
        (int(tenant["id"]),),
    ).fetchall()
    assert [row["role"] for row in rows] == ["admin", "user"]
    assert all(row["status"] == "active" for row in rows)

    dispatcher = _source("TenantRuntime/handlers.py")
    assert 'spec.role == "admin"' in dispatcher
    assert 'spec.role == "user"' in dispatcher
    assert "unsupported tenant bot role" in dispatcher

    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
    )
    customer = service.register_customer(
        91002,
        display_name="Shared Runtime User",
        username="runtime_user",
    )
    assert int(customer["tenant_id"]) == int(tenant["id"])

    other = factories.tenant(owner_telegram_id=92001)
    foreign = TenantBusinessService(
        conn,
        tenant_id=int(other["id"]),
        owner_telegram_id=92001,
        secret_cipher=cipher,
    )
    with pytest.raises(TenantBusinessError):
        foreign.customer(int(customer["id"]))


def test_stable_cross_phase_purchase_renew_expire_ticket_broadcast_backup_restore(
    conn, factories, cipher
) -> None:
    """One release-gate flow crosses the core AdminBot/UserBot business paths."""
    state = multinode_setup(conn, factories, cipher)
    service = state["service"]
    owner = int(state["owner"])
    customer_tid = int(state["customer"])
    tenant_id = int(state["tenant"]["id"])

    # Purchase: approved receipt -> real multi-node fulfillment.
    purchased = service.fulfill_paid_order(
        owner,
        order_id=int(state["order_id"]),
    )
    subscription_id = int(purchased["id"])
    assert purchased["status"] == "active"

    nodes = service.subscription_nodes_admin(
        owner,
        subscription_id=subscription_id,
    )
    assert len(nodes) == 3
    assert {row["provider_kind"] for row in nodes} == {
        "hiddify",
        "xui",
        "xnet",
    }
    assert all(row["status"] == "active" for row in nodes)

    # Renewal: make the real advanced window eligible from fresh global usage,
    # then use the same receipt/review/fulfillment path as UserBot.
    primary_endpoint = str(state["primary"]["endpoint"])
    state["panel"].usage_by_endpoint[primary_endpoint] = 19 * 1024**3
    eligibility = service.renewal_eligibility(
        customer_tid,
        subscription_id=subscription_id,
    )
    assert eligibility["allowed"] is True

    renewal_order = service.create_renewal_order(
        customer_tid,
        subscription_id=subscription_id,
        plan_id=int(state["plan"]["id"]),
    )
    method = service.list_payment_methods(customer_tid)[0]
    renewal_receipt = service.submit_receipt(
        customer_tid,
        order_id=int(renewal_order["id"]),
        method_id=int(method["id"]),
        reference="stable-renewal",
    )
    reviewed = service.review_receipt(
        owner,
        int(renewal_receipt["id"]),
        approve=True,
    )
    assert reviewed["operation"] == "renewal"
    renewed = service.fulfill_paid_order(
        owner,
        order_id=int(renewal_order["id"]),
    )
    assert renewed["operation"] == "renewal"
    assert renewed["status"] == "active"
    assert len(state["panel"].renew_calls) == 3

    # Expiry: force the renewed subscription due, then enforce every node.
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? "
        "WHERE tenant_id=? AND id=?",
        (
            iso_utc(utcnow() - timedelta(minutes=1)),
            tenant_id,
            subscription_id,
        ),
    )
    conn.commit()
    expiry = service.expire_due_subscriptions(owner)
    assert expiry == {"expired": 1, "errors": 0}
    expired = service.customer_subscription_status(
        customer_tid,
        subscription_id=subscription_id,
        refresh=False,
    )
    assert expired["status"] == "expired"
    expired_nodes = service.subscription_nodes_admin(
        owner,
        subscription_id=subscription_id,
    )
    assert all(row["status"] == "expired" for row in expired_nodes)

    # Ticket: UserBot creates a thread, AdminBot answers it.
    ticket = service.create_ticket(
        customer_tid,
        subject="Stable release",
        body="Verify support path",
        media=b"stable-user-image",
        media_mime="image/jpeg",
    )
    answered = service.reply_ticket_admin(
        owner,
        ticket_id=int(ticket["id"]),
        reply="Support path verified",
        media=b"stable-admin-image",
        media_mime="image/jpeg",
    )
    assert answered["status"] == "answered"
    thread = service.ticket_messages(
        customer_tid,
        ticket_id=int(ticket["id"]),
    )
    assert [item["sender_type"] for item in thread] == ["user", "admin"]

    # Broadcast: audit a real Tenant-targeted run and ensure targets stay local.
    run_id = service.start_broadcast_run_admin(
        owner,
        segment="all",
        message_kind="text",
        target_count=1,
        buttons_count=0,
    )
    service.finish_broadcast_run_admin(
        owner,
        run_id=run_id,
        sent=1,
        failed=0,
    )
    broadcast = conn.execute(
        "SELECT * FROM tenant_broadcast_runs WHERE tenant_id=? AND id=?",
        (tenant_id, run_id),
    ).fetchone()
    assert broadcast is not None
    assert int(broadcast["sent_count"]) == 1
    targets = service.broadcast_targets_admin(owner, segment="all")
    assert {int(item["telegram_user_id"]) for item in targets} == {
        customer_tid
    }

    # Create a second Tenant before backup/restore.  Its state must survive
    # untouched while the first Tenant is replaced transactionally.
    foreign_tenant = factories.tenant(owner_telegram_id=93001)
    foreign = TenantBusinessService(
        conn,
        tenant_id=int(foreign_tenant["id"]),
        owner_telegram_id=93001,
        secret_cipher=cipher,
    )
    foreign_customer = foreign.register_customer(
        93002,
        display_name="Foreign Stable User",
        username="foreign_stable",
    )

    artifact = create_tenant_backup(conn, tenant_id=tenant_id)
    assert artifact.table_count > 0
    assert artifact.row_count > 0

    local_customer = service.customer(customer_tid)
    conn.execute(
        "UPDATE tenant_customers SET display_name='BROKEN LOCAL STATE' "
        "WHERE tenant_id=? AND id=?",
        (tenant_id, int(local_customer["id"])),
    )
    conn.execute(
        "DELETE FROM tenant_ticket_messages "
        "WHERE tenant_id=? AND ticket_id=?",
        (tenant_id, int(ticket["id"])),
    )
    conn.commit()

    restored = restore_tenant_backup(
        conn,
        tenant_id=tenant_id,
        data=artifact.data,
    )
    assert restored.format == BACKUP_FORMAT
    assert restored.rows_restored == artifact.row_count
    assert service.customer(customer_tid)["display_name"] == "Buyer"
    restored_thread = service.ticket_messages(
        customer_tid,
        ticket_id=int(ticket["id"]),
    )
    assert [item["sender_type"] for item in restored_thread] == [
        "user",
        "admin",
    ]

    # Cross-Tenant data is neither deleted nor exposed by the restore.
    still_foreign = conn.execute(
        "SELECT display_name FROM tenant_customers "
        "WHERE tenant_id=? AND id=?",
        (int(foreign_tenant["id"]), int(foreign_customer["id"])),
    ).fetchone()
    assert still_foreign is not None
    assert still_foreign["display_name"] == "Foreign Stable User"
    with pytest.raises(TenantBusinessError):
        foreign.ticket_admin(93001, ticket_id=int(ticket["id"]))
