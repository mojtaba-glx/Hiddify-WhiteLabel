"""Runtime state is durable, scoped and rejects credentials."""

from __future__ import annotations

import pytest

from TenantRuntime.state import (
    MAX_STATE_BYTES,
    RuntimeStateError,
    StateScope,
    TenantStateStore,
    encode_state,
)


def test_same_user_is_isolated_by_tenant_and_role(conn, factories) -> None:
    first = factories.tenant()
    second = factories.tenant()
    stores = [
        TenantStateStore(conn, scope=StateScope(int(first["id"]), "admin")),
        TenantStateStore(conn, scope=StateScope(int(first["id"]), "user")),
        TenantStateStore(conn, scope=StateScope(int(second["id"]), "user")),
    ]
    for number, store in enumerate(stores, start=1):
        store.save(55, {"step": number})
    assert [store.load(55)["step"] for store in stores] == [1, 2, 3]
    assert len({store.state_key(55) for store in stores}) == 3


def test_clear_cannot_touch_another_scope(conn, factories) -> None:
    first = factories.tenant()
    second = factories.tenant()
    a = TenantStateStore(conn, scope=StateScope(int(first["id"]), "user"))
    b = TenantStateStore(conn, scope=StateScope(int(second["id"]), "user"))
    a.save(9, {"screen": "a"})
    b.save(9, {"screen": "b"})
    assert a.clear(9)
    assert a.load(9) == {} and b.load(9) == {"screen": "b"}


@pytest.mark.parametrize(
    "state",
    [
        {"bot_token": "hidden"},
        {"nested": [{"webhook-secret": "hidden"}]},
        {"message": "990001:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456"},
    ],
)
def test_state_rejects_credentials_at_any_depth(state) -> None:
    with pytest.raises(RuntimeStateError):
        encode_state(state)


def test_state_rejects_unknown_types_and_oversize() -> None:
    with pytest.raises(RuntimeStateError):
        encode_state({"bad": object()})
    with pytest.raises(RuntimeStateError):
        encode_state({"large": "x" * (MAX_STATE_BYTES + 1)})


def test_invalid_scope_and_user_ids_are_rejected(conn, factories) -> None:
    tenant = factories.tenant()
    with pytest.raises(RuntimeStateError):
        StateScope(0, "user")
    with pytest.raises(RuntimeStateError):
        StateScope(int(tenant["id"]), "owner")
    store = TenantStateStore(conn, scope=StateScope(int(tenant["id"]), "user"))
    with pytest.raises(RuntimeStateError):
        store.load(0)
