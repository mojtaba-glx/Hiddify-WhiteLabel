"""Provider-neutral panel boundary for tenant subscription provisioning.

Concrete panel integrations live in their own modules.  The runtime passes only
tenant-owned routing metadata plus the decrypted credential at the final call
boundary; credentials are never retained by adapters or returned to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


class PanelError(RuntimeError):
    """Safe panel failure; messages must never contain endpoints or secrets."""


@dataclass(frozen=True)
class PanelTarget:
    kind: str
    endpoint: str
    admin_path: str = ""
    user_path: str = ""


@dataclass(frozen=True)
class ProvisionRequest:
    tenant_id: int
    server_id: int
    subscription_id: int
    customer_id: int
    traffic_bytes: int
    duration_days: int
    expires_at: str
    idempotency_key: str


@dataclass(frozen=True)
class RenewRequest:
    traffic_bytes: int
    duration_days: int
    expires_at: str
    reset_usage: bool = True
    idempotency_key: str = ""


@dataclass(frozen=True)
class ProvisionResult:
    external_ref: str
    subscription_url: str = ""


@dataclass(frozen=True)
class PanelUserResult:
    external_ref: str
    usage_bytes: int
    active: bool
    traffic_bytes: int | None = None
    expires_at: str | None = None
    last_online: str | None = None
    subscription_url: str = ""


@dataclass(frozen=True)
class UsageResult:
    usage_bytes: int
    active: bool
    last_online: str | None = None


class PanelAdapter(Protocol):
    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult: ...

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult: ...

    def renew(
        self, *, target: PanelTarget, secret: str, external_ref: str, request: RenewRequest
    ) -> PanelUserResult: ...

    def set_enabled(
        self, *, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> PanelUserResult: ...

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None: ...

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult: ...

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str: ...


class UnconfiguredPanelAdapter:
    """Fail closed until an explicit provider adapter is selected."""

    @staticmethod
    def _fail() -> None:
        raise PanelError("panel adapter is not configured")

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        del target, secret, request
        self._fail()

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        del target, secret, external_ref
        self._fail()

    def renew(
        self, *, target: PanelTarget, secret: str, external_ref: str, request: RenewRequest
    ) -> PanelUserResult:
        del target, secret, external_ref, request
        self._fail()

    def set_enabled(
        self, *, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> PanelUserResult:
        del target, secret, external_ref, enabled
        self._fail()

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        del target, secret, external_ref
        self._fail()

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        del target, secret, external_ref
        self._fail()

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        del target, external_ref
        self._fail()


class RoutedPanelAdapter:
    """Dispatch to the provider selected by the tenant-owned server row."""

    def __init__(self, adapters: Mapping[str, PanelAdapter]) -> None:
        self._adapters = {
            str(kind).strip().lower(): adapter
            for kind, adapter in adapters.items()
            if str(kind).strip()
        }

    def _adapter(self, target: PanelTarget) -> PanelAdapter:
        adapter = self._adapters.get(str(target.kind).strip().lower())
        if adapter is None:
            raise PanelError("panel kind is not supported")
        return adapter

    def provision(
        self, *, target: PanelTarget, secret: str, request: ProvisionRequest
    ) -> ProvisionResult:
        return self._adapter(target).provision(target=target, secret=secret, request=request)

    def get_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> PanelUserResult:
        return self._adapter(target).get_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def renew(
        self, *, target: PanelTarget, secret: str, external_ref: str, request: RenewRequest
    ) -> PanelUserResult:
        return self._adapter(target).renew(
            target=target, secret=secret, external_ref=external_ref, request=request
        )

    def set_enabled(
        self, *, target: PanelTarget, secret: str, external_ref: str, enabled: bool
    ) -> PanelUserResult:
        return self._adapter(target).set_enabled(
            target=target, secret=secret, external_ref=external_ref, enabled=enabled
        )

    def delete_user(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> None:
        self._adapter(target).delete_user(
            target=target, secret=secret, external_ref=external_ref
        )

    def usage(
        self, *, target: PanelTarget, secret: str, external_ref: str
    ) -> UsageResult:
        return self._adapter(target).usage(
            target=target, secret=secret, external_ref=external_ref
        )

    def subscription_link(self, *, target: PanelTarget, external_ref: str) -> str:
        return self._adapter(target).subscription_link(
            target=target, external_ref=external_ref
        )


def build_default_panel_adapter() -> PanelAdapter:
    """Build the production router without importing optional providers at module load."""

    from TenantRuntime.hiddify import HiddifyPanelAdapter

    return RoutedPanelAdapter({"hiddify": HiddifyPanelAdapter()})
