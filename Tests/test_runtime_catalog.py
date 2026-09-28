"""Phase-5 bot catalog validates credentials and partitions fixed shards."""

from __future__ import annotations

from Database.repositories import BotRepository, TenantRepository, TenantRuntimeRepository
from Gateway.catalog import RuntimeCatalog
from Shared.crypto import FernetTokenCipher, generate_key


def _runtime_bot(conn, factories, cipher, *, role: str, telegram_id: int):
    tenant = factories.tenant(owner_telegram_id=telegram_id + 100)
    bot, _ = factories.bot(int(tenant["id"]), role, cipher)
    row = BotRepository(conn).set_telegram_identity(
        int(bot["id"]), telegram_bot_id=telegram_id, telegram_username=f"bot_{telegram_id}"
    )
    assert row is not None
    return tenant, row


def test_shards_partition_all_runtime_bots_without_overlap(conn, factories, cipher) -> None:
    rows = [
        _runtime_bot(conn, factories, cipher, role="admin" if i % 2 else "user", telegram_id=800000 + i)
        for i in range(1, 9)
    ]
    snapshots = [
        RuntimeCatalog(conn, cipher=cipher, shard_count=4, shard_index=index).load()
        for index in range(4)
    ]
    groups = [{spec.bot_id for spec in snapshot.specs} for snapshot in snapshots]
    assert set.union(*groups) == {int(bot["id"]) for _, bot in rows}
    assert sum(len(group) for group in groups) == len(set.union(*groups))
    for index, group in enumerate(groups):
        assert all(bot_id % 4 == index for bot_id in group)


def test_catalog_spec_repr_never_contains_token_or_ciphertext(conn, factories, cipher) -> None:
    _, bot = _runtime_bot(conn, factories, cipher, role="user", telegram_id=810001)
    row = BotRepository(conn).get_by_id(int(bot["id"]))
    token = cipher.decrypt(row["encrypted_token"])
    spec = RuntimeCatalog(conn, cipher=cipher, shard_count=1, shard_index=0).load().specs[0]
    rendered = repr(spec)
    assert token not in rendered
    assert row["encrypted_token"] not in rendered
    assert row["token_fingerprint"] not in rendered


def test_corrupt_credential_is_rejected_without_hiding_healthy_bot(conn, factories, cipher) -> None:
    _, healthy = _runtime_bot(conn, factories, cipher, role="admin", telegram_id=820001)
    _, corrupt = _runtime_bot(conn, factories, cipher, role="user", telegram_id=820002)
    other_cipher = FernetTokenCipher(generate_key())
    # Valid Fernet shape, wrong key/fingerprint: catalog must fail this row closed.
    conn.execute(
        "UPDATE tenant_bots SET encrypted_token=? WHERE id=?",
        (other_cipher.encrypt("990001:FAKE_catalog_corruption_abcdefghijklmnopqrstuvwxyz"), corrupt["id"]),
    )
    snapshot = RuntimeCatalog(conn, cipher=cipher, shard_count=1, shard_index=0).load()
    assert {spec.bot_id for spec in snapshot.specs} == {int(healthy["id"])}
    assert snapshot.rejected_bot_ids == (int(corrupt["id"]),)


def test_catalog_excludes_disabled_tenant_runtime_and_bot(conn, factories, cipher) -> None:
    entries = [
        _runtime_bot(conn, factories, cipher, role="user", telegram_id=830000 + i)
        for i in range(3)
    ]
    TenantRepository(conn).update_status(int(entries[0][0]["id"]), "disabled")
    TenantRuntimeRepository(conn).update_status(int(entries[1][0]["id"]), "disabled")
    BotRepository(conn).update_status(int(entries[2][1]["id"]), "disabled")
    snapshot = RuntimeCatalog(conn, cipher=cipher, shard_count=1, shard_index=0).load()
    assert snapshot.specs == () and snapshot.rejected_bot_ids == ()


def test_catalog_validates_shard_bounds(conn, cipher) -> None:
    for count, index in ((0, 0), (65, 0), (4, -1), (4, 4)):
        try:
            RuntimeCatalog(conn, cipher=cipher, shard_count=count, shard_index=index)
        except ValueError:
            continue
        raise AssertionError("invalid shard accepted")
