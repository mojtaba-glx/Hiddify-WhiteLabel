"""Safe panel-adapter boundary for tenant subscription provisioning.

No concrete Hiddify/X-UI HTTP dialect is guessed here. Panel versions and
authentication paths differ, so production adapters must implement this small
contract and be tested against the target panel. The default adapter fails
closed and never exposes a stored credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PanelError(RuntimeError):
    """Safe panel failure; it never contains endpoint or secret values."""


@dataclass(frozen=True)
class ProvisionRequest:
    tenant_id: int
    server_id: int
    subscription_id: int
    customer_id: int
    traffic_bytes: int
    expires_at: str
    idempotency_key: str


@dataclass(frozen=True)
class ProvisionResult:
    external_ref: str


@dataclass(frozen=True)
class UsageResult:
    usage_bytes: int
    active: bool


class PanelAdapter(Protocol):
    def provision(self, *, endpoint: str, secret: str, request: ProvisionRequest) -> ProvisionResult: ...
    def usage(self, *, endpoint: str, secret: str, external_ref: str) -> UsageResult: ...


class UnconfiguredPanelAdapter:
    """Default: blocks activation until an explicit provider adapter exists."""
    def provision(self, *, endpoint: str, secret: str, request: ProvisionRequest) -> ProvisionResult:
        del endpoint, secret, request
        raise PanelError("panel adapter is not configured")

    def usage(self, *, endpoint: str, secret: str, external_ref: str) -> UsageResult:
        del endpoint, secret, external_ref
        raise PanelError("panel adapter is not configured")
