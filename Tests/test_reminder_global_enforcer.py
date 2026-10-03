"""Global enforcer, frozen-node accounting and durable reminder tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.business import TenantBusinessService
from TenantRuntime.lifecycle import TenantLifecycleCoordinator
from TenantRuntime.panels import (
    PanelError,
    PanelTarget,
    PanelUserResult,
    ProvisionRequest,
    ProvisionResult,
    RenewRequest,
    UsageResult,
)
from TenantRuntime.reminders import (
    claim_due_deliveries,
    enqueue_due_reminders,
    finish_delivery,
)


class EnforcerPanel:
    def __init__(self) -> None:
        self.users: dict[tuple[str, str], dict] = {}
        self.fail_usage_endpoints: set[str] = set()
        self.fail_disable_endpoints: set[str] = set()
        self.disable_calls: list[tuple[str, str]] = []

    @staticmethod
    def _key(target: PanelTarget, external_ref: str) -> tuple[str, str]:
        return target.endpoint, external_ref

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        assert secret
        ref = f"ref-{request.server_id}-{request.subscription_id}"
        self.users[self._key(target, ref)] = {
            "usage": 0,
            "active": True,
            "traffic": request.traffic_bytes,
            "last_online": None,
        }
        return ProvisionResult(
            external_ref=ref,
            subscription_url=self.subscription_link(
                target=target, external_ref=ref
            ),
        )

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        assert secret
        row = self.users[self._key(target, external_ref)]
        return PanelUserResult(
            external_ref=external_ref,
            usage_bytes=int(row["usage"]),
            active=bool(row["active"]),
            traffic_bytes=int(row["traffic"]),
            last_online=row["last_online"],
            subscription_url=self.subscription_link(
                target=target, external_ref=external_ref
            ),
        )

    def renew(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        request: RenewRequest,
    ) -> PanelUserResult:
        row = self.users[self._key(target, external_ref)]
        row["usage"] = 0
        row["active"] = True
        row["traffic"] = request.traffic_bytes
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def set_enabled(
        self,
        *,
        target: PanelTarget,
        secret: str,
        external_ref: str,
        enabled: bool,
    ) -> PanelUserResult:
        assert secret
        if not enabled and target.endpoint in self.fail_disable_endpoints:
            raise PanelError("disable unavailable")
        if not enabled:
            self.disable_calls.append((target.endpoint, external_ref))
        row = self.users[self._key(target, external_ref)]
        row["active"] = bool(enabled)
        return self.get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        self.users.pop(self._key(target, external_ref), None)

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        assert secret
        if target.endpoint in self.fail_usage_endpoints:
            raise PanelError("usage unavailable")
        row = self.users[self._key(target, external_ref)]
        return UsageResult(
            usage_bytes=int(row["usage"]),
            active=bool(row["active"]),
            last_online=row["last_online"],
        )

    def subscription_link(
        self, *, target: PanelTarget, external_ref: str
    ) -> str:
        return f"{target.endpoint}/sub/{external_ref}"

    def subscription_content(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> str:
        return f"vless://{external_ref}@edge.example:443?type=tcp#{target.kind}"


class FakeReminderSender:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.deliveries = []
        self.fail_first = bool(fail_first)

    async def send(self, delivery) -> None:
        self.deliveries.append(delivery)
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("temporary telegram failure")


def _setup(conn, factories, cipher):
    owner, customer = 7001, 31
    tenant = factories.tenant(owner_telegram_id=owner)
    factories.bot(int(tenant["id"]), "user", cipher)
    panel = EnforcerPanel()
    service = TenantBusinessService(
        conn,
        tenant_id=int(tenant["id"]),
        owner_telegram_id=owner,
        secret_cipher=cipher,
        panel_adapter=panel,
    )
    primary = service.add_server(
        owner,
        label="TR",
        panel_kind="hiddify",
        endpoint="https://tr.example",
        admin_path="admin",
        user_path="user",
    )
    child = service.add_server(
        owner,
        label="DE",
        panel_kind="hiddify",
        endpoint="https://de.example",
        admin_path="admin",
        user_path="user",
    )
    service.set_panel_credential(
        owner, server_id=int(primary["id"]), secret="tr-secret"
    )
    service.set_panel_credential(
        owner, server_id=int(child["id"]), secret="de-secret"
    )
    service.set_default_server(owner, server_id=int(primary["id"]))
    service.add_node(
        owner, label="Germany", server_id=int(child["id"]), location="DE"
    )
    plan = service.add_plan(
        owner,
        name="20GB",
        traffic_gb=20,
        duration_days=30,
        price=1000,
        currency="IRR",
    )
    method = service.add_payment_method(
        owner,
        kind="card",
        title="Card",
        currency="IRR",
        destination="1111",
    )
    service.register_customer(customer, display_name="Buyer", username=None)
    order = service.create_order(customer, int(plan["id"]))
    receipt = service.submit_receipt(
        customer,
        order_id=int(order["id"]),
        method_id=int(method["id"]),
        reference="ref",
    )
    reviewed = service.review_receipt(owner, int(receipt["id"]), approve=True)
    service.fulfill_paid_order(owner, order_id=int(reviewed["order_id"]))
    subscription = service.list_subscriptions(customer)[0]
    nodes = service.subscription_nodes_admin(
        owner, subscription_id=int(subscription["id"])
    )
    by_endpoint = {}
    for node in nodes:
        server = service.server(int(node["server_id"]))
        by_endpoint[str(server["endpoint"])] = node
    return {
        "owner": owner,
        "customer": customer,
        "tenant": tenant,
        "service": service,
        "panel": panel,
        "subscription": subscription,
        "nodes": by_endpoint,
    }


def test_failed_child_usage_keeps_frozen_snapshot_in_global_total(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    tr = state["nodes"]["https://tr.example"]
    de = state["nodes"]["https://de.example"]
    state["panel"].users[("https://tr.example", str(tr["external_ref"]))]["usage"] = 5 * 1024**3
    state["panel"].users[("https://de.example", str(de["external_ref"]))]["usage"] = 4 * 1024**3

    first = state["service"].sync_subscription_usage(
        state["owner"], subscription_id=sub_id
    )
    assert first["usage_bytes"] == 9 * 1024**3

    state["panel"].fail_usage_endpoints.add("https://de.example")
    second = state["service"].sync_subscription_usage(
        state["owner"],
        subscription_id=sub_id,
        freeze_after=1,
    )
    assert second["usage_bytes"] == 9 * 1024**3
    node = conn.execute(
        "SELECT fail_count, frozen_at, usage_bytes FROM tenant_subscription_nodes "
        "WHERE tenant_id=? AND subscription_id=? AND server_id=?",
        (
            int(state["tenant"]["id"]),
            sub_id,
            int(de["server_id"]),
        ),
    ).fetchone()
    assert int(node["fail_count"]) >= 1
    assert node["frozen_at"]
    assert int(node["usage_bytes"]) == 4 * 1024**3


def test_quota_enforcement_stays_pending_until_every_node_is_disabled(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    tr = state["nodes"]["https://tr.example"]
    de = state["nodes"]["https://de.example"]
    # Establish a real last-known snapshot before the child becomes unreachable.
    state["panel"].users[("https://tr.example", str(tr["external_ref"]))]["usage"] = 5 * 1024**3
    state["panel"].users[("https://de.example", str(de["external_ref"]))]["usage"] = 4 * 1024**3
    baseline = state["service"].sync_subscription_usage(
        state["owner"], subscription_id=sub_id
    )
    assert baseline["usage_bytes"] == 9 * 1024**3

    state["panel"].users[("https://tr.example", str(tr["external_ref"]))]["usage"] = 17 * 1024**3
    state["panel"].fail_usage_endpoints.add("https://de.example")
    state["panel"].fail_disable_endpoints.add("https://de.example")

    pending = state["service"].sync_subscription_usage(
        state["owner"], subscription_id=sub_id, freeze_after=1
    )
    assert pending["usage_bytes"] == 21 * 1024**3
    assert pending["status"] == "active"
    assert pending["enforcement_pending"] is True
    stored = conn.execute(
        "SELECT status, enforcement_pending, enforcement_error "
        "FROM tenant_subscriptions WHERE id=?",
        (sub_id,),
    ).fetchone()
    assert stored["status"] == "active"
    assert int(stored["enforcement_pending"]) == 1
    assert "disable pending" in str(stored["enforcement_error"])

    state["panel"].fail_disable_endpoints.clear()
    done = state["service"].sync_subscription_usage(
        state["owner"], subscription_id=sub_id, freeze_after=1
    )
    assert done["status"] == "expired"
    assert done["enforcement_pending"] is False
    nodes = state["service"].subscription_nodes_admin(
        state["owner"], subscription_id=sub_id
    )
    assert all(row["status"] == "expired" for row in nodes)


def test_reminders_are_period_scoped_and_deduplicated(
    conn, db_path, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    expires = iso_utc(utcnow() + timedelta(days=2, minutes=5))
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=?, usage_bytes=? "
        "WHERE id=? AND tenant_id=?",
        (
            expires,
            18 * 1024**3,
            sub_id,
            int(state["tenant"]["id"]),
        ),
    )
    sender = FakeReminderSender()
    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        panel_adapter_factory=lambda: state["panel"],
        reminder_sender=sender,
        reminder_days=3,
        reminder_remaining_gb=3,
    )
    # Keep panel counters aligned with the stored reminder snapshot.
    tr = state["nodes"]["https://tr.example"]
    state["panel"].users[("https://tr.example", str(tr["external_ref"]))]["usage"] = 18 * 1024**3

    first = asyncio.run(coordinator.run_once())
    assert first.reminders_sent == 2
    assert {d.event_type for d in sender.deliveries} == {"days", "usage"}

    second = asyncio.run(coordinator.run_once())
    assert second.reminders_sent == 0
    assert len(sender.deliveries) == 2

    sent = conn.execute(
        "SELECT event_type, status FROM tenant_subscription_notifications "
        "WHERE tenant_id=? AND subscription_id=? ORDER BY event_type",
        (int(state["tenant"]["id"]), sub_id),
    ).fetchall()
    assert {row["event_type"] for row in sent} == {"days", "usage"}
    assert all(row["status"] == "sent" for row in sent)


def test_lifecycle_stops_already_queued_reminders_when_admin_disables_them(
    conn, db_path, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    tenant_id = int(state["tenant"]["id"])
    now = utcnow()
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=?, usage_bytes=? "
        "WHERE id=? AND tenant_id=?",
        (
            iso_utc(now + timedelta(days=2, minutes=5)),
            18 * 1024**3,
            sub_id,
            tenant_id,
        ),
    )
    tr = state["nodes"]["https://tr.example"]
    state["panel"].users[
        ("https://tr.example", str(tr["external_ref"]))
    ]["usage"] = 18 * 1024**3

    queued = enqueue_due_reminders(
        conn,
        tenant_id=tenant_id,
        days_threshold=3,
        remaining_gb_threshold=3,
        now=now,
    )
    assert queued == 2
    state["service"].set_userbot_setting_admin(
        state["owner"],
        key="reminder_enabled",
        value=False,
    )

    sender = FakeReminderSender()
    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        panel_adapter_factory=lambda: state["panel"],
        reminder_sender=sender,
    )
    report = asyncio.run(coordinator.run_once())
    assert report.reminders_sent == 0
    assert report.reminders_skipped >= 2
    assert sender.deliveries == []

    queued = conn.execute(
        "SELECT status FROM tenant_subscription_notifications "
        "WHERE tenant_id=? AND subscription_id=? "
        "AND status IN ('pending','failed','processing')",
        (tenant_id, sub_id),
    ).fetchall()
    assert queued == []


def test_lifecycle_discards_queued_events_outside_new_admin_thresholds(
    conn, db_path, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    tenant_id = int(state["tenant"]["id"])
    now = utcnow()
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=?, usage_bytes=? "
        "WHERE id=? AND tenant_id=?",
        (
            iso_utc(now + timedelta(days=2, minutes=5)),
            18 * 1024**3,
            sub_id,
            tenant_id,
        ),
    )
    tr = state["nodes"]["https://tr.example"]
    state["panel"].users[
        ("https://tr.example", str(tr["external_ref"]))
    ]["usage"] = 18 * 1024**3

    assert enqueue_due_reminders(
        conn,
        tenant_id=tenant_id,
        days_threshold=3,
        remaining_gb_threshold=3,
        now=now,
    ) == 2
    state["service"].set_userbot_setting_admin(
        state["owner"],
        key="reminder_days",
        value=1,
    )
    state["service"].set_userbot_setting_admin(
        state["owner"],
        key="reminder_remaining_gb",
        value=1,
    )

    sender = FakeReminderSender()
    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        panel_adapter_factory=lambda: state["panel"],
        reminder_sender=sender,
    )
    report = asyncio.run(coordinator.run_once())
    assert report.reminders_sent == 0
    assert report.reminders_skipped >= 2
    assert sender.deliveries == []


def test_expired_notice_is_sent_once_after_verified_enforcement(
    conn, db_path, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? "
        "WHERE id=? AND tenant_id=?",
        (
            iso_utc(utcnow() - timedelta(minutes=1)),
            sub_id,
            int(state["tenant"]["id"]),
        ),
    )
    sender = FakeReminderSender()
    coordinator = TenantLifecycleCoordinator(
        db_path=db_path,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        panel_adapter_factory=lambda: state["panel"],
        reminder_sender=sender,
    )
    first = asyncio.run(coordinator.run_once())
    assert first.expired == 1
    assert first.reminders_sent == 1
    assert sender.deliveries[-1].event_type == "expired"

    second = asyncio.run(coordinator.run_once())
    assert second.reminders_sent == 0


def test_failed_reminder_delivery_is_retried_with_backoff(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    now = utcnow()
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=?",
        (iso_utc(now + timedelta(days=1)), sub_id),
    )
    assert enqueue_due_reminders(
        conn,
        tenant_id=int(state["tenant"]["id"]),
        days_threshold=3,
        remaining_gb_threshold=3,
        now=now,
    ) >= 1
    deliveries, claimed = claim_due_deliveries(
        conn,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        lease_seconds=60,
        max_retries=3,
        retry_base_seconds=10,
        now=now,
    )
    day_delivery = next(d for d in deliveries if d.event_type == "days")
    failed = finish_delivery(
        conn,
        event_key=day_delivery.event_key,
        success=False,
        max_retries=3,
        retry_base_seconds=10,
        now=now,
    )
    assert failed.retried == 1

    immediate, _ = claim_due_deliveries(
        conn,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        lease_seconds=60,
        max_retries=3,
        retry_base_seconds=10,
        now=now + timedelta(seconds=5),
    )
    assert all(d.event_key != day_delivery.event_key for d in immediate)

    later, report = claim_due_deliveries(
        conn,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        lease_seconds=60,
        max_retries=3,
        retry_base_seconds=10,
        now=now + timedelta(seconds=11),
    )
    assert any(d.event_key == day_delivery.event_key for d in later)
    assert report.requeued >= 1

def test_old_period_warning_is_skipped_after_renewal_change(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    now = utcnow()
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=?",
        (iso_utc(now + timedelta(days=1)), sub_id),
    )
    assert enqueue_due_reminders(
        conn,
        tenant_id=int(state["tenant"]["id"]),
        days_threshold=3,
        remaining_gb_threshold=3,
        now=now,
    ) >= 1
    conn.execute(
        "UPDATE tenant_subscriptions SET expires_at=? WHERE id=?",
        (iso_utc(now + timedelta(days=30)), sub_id),
    )
    deliveries, report = claim_due_deliveries(
        conn,
        cipher=cipher,
        shard_count=1,
        shard_index=0,
        lease_seconds=60,
        max_retries=3,
        retry_base_seconds=10,
        now=now,
    )
    assert deliveries == []
    assert report.skipped >= 1


def test_successful_renewal_clears_pending_enforcement_state(
    conn, factories, cipher
) -> None:
    state = _setup(conn, factories, cipher)
    sub_id = int(state["subscription"]["id"])
    conn.execute(
        "UPDATE tenant_subscriptions "
        "SET enforcement_pending=1, enforcement_error='old pending' WHERE id=?",
        (sub_id,),
    )
    state["service"].renew_subscription(
        state["owner"],
        subscription_id=sub_id,
        traffic_gb=30,
        duration_days=30,
        idempotency_key="renew-clear-enforcer",
    )
    stored = conn.execute(
        "SELECT enforcement_pending, enforcement_error, enforced_at "
        "FROM tenant_subscriptions WHERE id=?",
        (sub_id,),
    ).fetchone()
    assert int(stored["enforcement_pending"]) == 0
    assert stored["enforcement_error"] is None
    assert stored["enforced_at"] is None

