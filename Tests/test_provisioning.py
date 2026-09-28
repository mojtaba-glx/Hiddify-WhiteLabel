"""Phase-4 atomic provisioning, one-time secrets and reversible lifecycle."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from Database.repositories import (
    AuditRepository,
    BotRepository,
    LicenseRepository,
    PlanRepository,
    TenantRepository,
    TenantRuntimeRepository,
)
from LicenseService.runtime import TenantRuntimeGate
from MasterBot.service import BotIdentity
from Provisioning.service import PreparedBot, ProvisioningError, TenantProvisioner
from Shared.access import AccessDenied
from Shared.crypto import FernetTokenCipher, generate_key, verify_webhook_secret
from Shared.timeutils import iso_utc, utcnow
from Tests.conftest import make_fake_token


class MappingVerifier:
    def __init__(self) -> None:
        self.identities: dict[str, BotIdentity] = {}
        self.fail = False

    async def verify(self, token: str) -> BotIdentity:
        if self.fail or token not in self.identities:
            raise RuntimeError(f"rejected {token}")
        return self.identities[token]


def _setup(conn):
    verifier = MappingVerifier()
    cipher = FernetTokenCipher(generate_key())
    gate = TenantRuntimeGate(conn, ttl_seconds=60)
    service = TenantProvisioner(
        conn,
        master_admin_id=9001,
        cipher=cipher,
        bot_verifier=verifier,
        cache_invalidator=gate.invalidate,
    )
    return service, verifier, cipher, gate


def _tokens(verifier: MappingVerifier, suffix: str = "one") -> tuple[str, str]:
    admin = make_fake_token(f"admin-{suffix}")
    user = make_fake_token(f"user-{suffix}")
    base = len(verifier.identities) + 700000
    verifier.identities[admin] = BotIdentity(base + 1, f"admin_{suffix}")
    verifier.identities[user] = BotIdentity(base + 2, f"user_{suffix}")
    return admin, user


def _provision(service, verifier, suffix="one"):
    admin, user = _tokens(verifier, suffix)
    result = asyncio.run(
        service.provision(
            9001,
            name=f"Tenant {suffix}",
            slug=f"tenant-{suffix}",
            owner_telegram_id=800001,
            admin_token=admin,
            user_token=user,
        )
    )
    return result, admin, user


def test_complete_provision_is_atomic_ready_and_secret_safe(conn) -> None:
    service, verifier, cipher, _ = _setup(conn)
    result, admin_token, user_token = _provision(service, verifier)
    runtime = TenantRuntimeRepository(conn).get_by_tenant(result.tenant_id)
    bots = BotRepository(conn).list_by_tenant(result.tenant_id)
    assert runtime is not None and runtime["status"] == "ready"
    assert runtime["data_namespace"] == result.data_namespace
    assert len(bots) == 2 and {row["role"] for row in bots} == {"admin", "user"}
    assert service.status(9001, result.tenant_id).ready
    by_role = {row["role"]: row for row in bots}
    assert cipher.decrypt(by_role["admin"]["encrypted_token"]) == admin_token
    assert cipher.decrypt(by_role["user"]["encrypted_token"]) == user_token
    assert verify_webhook_secret(
        result.webhook_secrets.admin, by_role["admin"]["webhook_secret_hash"]
    )
    assert verify_webhook_secret(
        result.webhook_secrets.user, by_role["user"]["webhook_secret_hash"]
    )
    dump = "\n".join(conn.iterdump())
    assert admin_token not in dump and user_token not in dump
    assert result.webhook_secrets.admin not in dump
    assert result.webhook_secrets.user not in dump
    rendered = repr(result)
    assert admin_token not in rendered and user_token not in rendered
    assert result.webhook_secrets.admin not in rendered
    assert result.webhook_secrets.user not in rendered
    audits = AuditRepository(conn).list_by_tenant(result.tenant_id)
    assert [item["action"] for item in audits] == ["tenant.provision"]


def test_duplicate_existing_token_rolls_back_entire_second_tenant(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    _, admin, _ = _provision(service, verifier, "first")
    second_user = make_fake_token("second-user")
    verifier.identities[second_user] = BotIdentity(799991, "second_user")
    before = (
        TenantRepository(conn).count(),
        conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM tenant_runtime_configs").fetchone()[0],
    )
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(
            service.provision(
                9001,
                name="Must Roll Back",
                slug="must-roll-back",
                owner_telegram_id=800002,
                admin_token=admin,
                user_token=second_user,
            )
        )
    after = (
        TenantRepository(conn).count(),
        conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM tenant_runtime_configs").fetchone()[0],
    )
    assert after == before
    assert TenantRepository(conn).get_by_slug("must-roll-back") is None


def test_duplicate_identity_between_roles_writes_nothing(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    admin, user = _tokens(verifier, "same-id")
    verifier.identities[user] = BotIdentity(
        verifier.identities[admin].telegram_bot_id, "same_identity"
    )
    with pytest.raises(ProvisioningError):
        asyncio.run(
            service.provision(
                9001,
                name="Invalid",
                slug="invalid-identities",
                owner_telegram_id=1,
                admin_token=admin,
                user_token=user,
            )
        )
    assert TenantRepository(conn).count() == 0


def test_second_bot_insert_failure_rolls_back_first_bot_and_namespace(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    existing, _, existing_user = _provision(service, verifier, "existing")
    admin = make_fake_token("new-admin")
    user = make_fake_token("new-user-duplicate-id")
    verifier.identities[admin] = BotIdentity(812341, "new_admin")
    verifier.identities[user] = BotIdentity(
        verifier.identities[existing_user].telegram_bot_id, "duplicate_user"
    )
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(
            service.provision(
                9001,
                name="Second Rollback",
                slug="second-rollback",
                owner_telegram_id=2,
                admin_token=admin,
                user_token=user,
            )
        )
    assert TenantRepository(conn).count() == 1
    assert conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tenant_runtime_configs").fetchone()[0] == 1
    assert service.status(9001, existing.tenant_id).ready


def test_verification_failure_is_safe_and_writes_nothing(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    token = make_fake_token("rejected")
    verifier.fail = True
    with pytest.raises(ProvisioningError) as captured:
        asyncio.run(service.prepare_bot(9001, role="admin", plain_token=token))
    assert token not in str(captured.value)
    assert TenantRepository(conn).count() == 0


def test_tampered_prepared_credential_is_rejected_before_write(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    admin_token, user_token = _tokens(verifier, "tamper")
    admin = asyncio.run(service.prepare_bot(9001, role="admin", plain_token=admin_token))
    user = asyncio.run(service.prepare_bot(9001, role="user", plain_token=user_token))
    tampered = PreparedBot(
        role=admin.role,
        telegram_bot_id=admin.telegram_bot_id,
        telegram_username=admin.telegram_username,
        token_tail="AAAA",
        encrypted_token=admin.encrypted_token,
        token_fingerprint=admin.token_fingerprint,
    )
    with pytest.raises(ProvisioningError):
        service.provision_prepared(
            9001,
            name="Tamper",
            slug="tamper-test",
            owner_telegram_id=3,
            admin_bot=tampered,
            user_bot=user,
        )
    assert TenantRepository(conn).count() == 0


def test_disable_enable_preserves_all_data_and_respects_runtime_gate(conn) -> None:
    service, verifier, _, gate = _setup(conn)
    result, _, _ = _provision(service, verifier, "lifecycle")
    plan = PlanRepository(conn).create(
        name="Runtime", duration_days=30, price=0, max_servers=1, max_users=1
    )
    now = utcnow()
    LicenseRepository(conn).create(
        tenant_id=result.tenant_id,
        plan_id=int(plan["id"]),
        status="active",
        starts_at=iso_utc(now),
        expires_at=iso_utc(now.replace(year=now.year + 1)),
    )
    before = {
        "bots": conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0],
        "licenses": conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0],
        "namespace": result.data_namespace,
    }
    assert gate.is_allowed(result.tenant_id)
    disabled = service.set_enabled(9001, result.tenant_id, enabled=False)
    assert not disabled.ready and disabled.runtime_status == "disabled"
    assert not gate.is_allowed(result.tenant_id)
    enabled = service.set_enabled(9001, result.tenant_id, enabled=True)
    assert enabled.ready and gate.is_allowed(result.tenant_id)
    after = {
        "bots": conn.execute("SELECT COUNT(*) FROM tenant_bots").fetchone()[0],
        "licenses": conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0],
        "namespace": TenantRuntimeRepository(conn).get_by_tenant(result.tenant_id)["data_namespace"],
    }
    assert after == before


def test_enable_requires_both_active_bots_and_rolls_back(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    result, _, _ = _provision(service, verifier, "disabled-bot")
    service.set_enabled(9001, result.tenant_id, enabled=False)
    admin = BotRepository(conn).get_by_tenant_role(result.tenant_id, "admin")
    BotRepository(conn).update_status(int(admin["id"]), "disabled")
    with pytest.raises(ProvisioningError):
        service.set_enabled(9001, result.tenant_id, enabled=True)
    tenant = TenantRepository(conn).get_by_id(result.tenant_id)
    runtime = TenantRuntimeRepository(conn).get_by_tenant(result.tenant_id)
    assert tenant["status"] == "disabled" and runtime["status"] == "disabled"


def test_enrolling_partial_tenant_creates_namespace_and_becomes_ready(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    tenant = TenantRepository(conn).create(
        name="Partial", slug="partial-one", owner_telegram_id=11
    )
    admin, user = _tokens(verifier, "partial")
    first = asyncio.run(
        service.enroll_bot(9001, int(tenant["id"]), role="admin", plain_token=admin)
    )
    assert not service.status(9001, int(tenant["id"])).ready
    assert TenantRuntimeRepository(conn).get_by_tenant(int(tenant["id"]))["status"] == "provisioning"
    second = asyncio.run(
        service.enroll_bot(9001, int(tenant["id"]), role="user", plain_token=user)
    )
    assert service.status(9001, int(tenant["id"])).ready
    assert first.webhook_secret != second.webhook_secret


def test_webhook_rotation_invalidates_old_secret_and_is_audited(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    result, _, _ = _provision(service, verifier, "rotate")
    old = result.webhook_secrets.admin
    rotated = service.rotate_webhook_secret(9001, result.tenant_id, role="admin")
    row = BotRepository(conn).get_by_tenant_role(result.tenant_id, "admin")
    assert not verify_webhook_secret(old, row["webhook_secret_hash"])
    assert verify_webhook_secret(rotated.webhook_secret, row["webhook_secret_hash"])
    assert rotated.webhook_secret not in repr(rotated)
    assert AuditRepository(conn).list_by_tenant(result.tenant_id)[0]["action"] == "tenant_bot.webhook_rotate"


def test_tenant_namespaces_are_unique_and_scoped(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    first, _, _ = _provision(service, verifier, "space-one")
    second, _, _ = _provision(service, verifier, "space-two")
    assert first.data_namespace != second.data_namespace
    repo = TenantRuntimeRepository(conn)
    assert int(repo.get_by_namespace(first.data_namespace)["tenant_id"]) == first.tenant_id
    assert int(repo.get_by_namespace(second.data_namespace)["tenant_id"]) == second.tenant_id


def test_all_provisioning_entry_points_are_owner_gated(conn) -> None:
    service, verifier, _, _ = _setup(conn)
    token, _ = _tokens(verifier, "denied")
    with pytest.raises(AccessDenied):
        asyncio.run(service.prepare_bot(12, role="admin", plain_token=token))
    with pytest.raises(AccessDenied):
        service.status(12, 1)
    with pytest.raises(AccessDenied):
        service.set_enabled(12, 1, enabled=False)
    with pytest.raises(AccessDenied):
        service.rotate_webhook_secret(12, 1, role="admin")
