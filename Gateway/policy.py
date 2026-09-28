"""Per-update tenant, bot, owner and license authorization."""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

from Database.repositories import BotRepository
from Gateway.catalog import RuntimeBotSpec
from LicenseService.runtime import GateDecision, TenantRuntimeGate


@dataclass(frozen=True)
class RuntimePolicyDecision:
    allowed: bool
    reason: str
    license_status: str = "unknown"


class RuntimePolicy:
    """Fail closed and re-read mutable routing fields for every update."""

    def __init__(self, conn: sqlite3.Connection, *, gate: TenantRuntimeGate) -> None:
        self.conn = conn
        self.gate = gate

    @staticmethod
    def _matches(spec: RuntimeBotSpec, current: dict) -> bool:
        try:
            return (
                int(current["bot_id"]) == spec.bot_id
                and int(current["tenant_id"]) == spec.tenant_id
                and str(current["role"]) == spec.role
                and int(current["telegram_bot_id"]) == spec.telegram_bot_id
                and str(current["data_namespace"]) == spec.data_namespace
                and hmac.compare_digest(
                    str(current["token_fingerprint"]), spec.token_fingerprint
                )
            )
        except Exception:
            return False

    def check(
        self, spec: RuntimeBotSpec, *, telegram_user_id: int | None
    ) -> RuntimePolicyDecision:
        try:
            user_id = int(telegram_user_id or 0)
            if user_id <= 0:
                return RuntimePolicyDecision(False, "missing_user")
            current = BotRepository(self.conn).get_runtime_candidate(spec.bot_id)
            if current is None or not self._matches(spec, current):
                return RuntimePolicyDecision(False, "bot_route_changed")
            if spec.role == "admin" and user_id != int(current["owner_telegram_id"]):
                return RuntimePolicyDecision(False, "admin_access_denied")
            gate: GateDecision = self.gate.check(spec.tenant_id)
            if not gate.allowed:
                return RuntimePolicyDecision(False, gate.reason, gate.status)
            return RuntimePolicyDecision(True, "allowed", gate.status)
        except Exception:
            return RuntimePolicyDecision(False, "policy_error")
