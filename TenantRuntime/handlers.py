"""Role dispatcher for tenant bot handlers.

Role-specific implementation lives in TenantRuntime/AdminBot and
TenantRuntime/UserBot. This module is intentionally small and keeps the legacy
runtime_access_gate import stable for callers/tests.
"""

from telegram.ext import Application

from Gateway.catalog import RuntimeBotSpec
from TenantRuntime.AdminBot.handlers import register_admin_handlers
from TenantRuntime.UserBot.handlers import register_user_handlers
from TenantRuntime.common import runtime_access_gate, runtime_error


def register_runtime_handlers(application: Application) -> None:
    spec = application.bot_data.get("runtime_spec")
    if not isinstance(spec, RuntimeBotSpec):
        raise RuntimeError("runtime bot specification is unavailable")
    if spec.role == "admin":
        register_admin_handlers(application)
        return
    if spec.role == "user":
        register_user_handlers(application)
        return
    raise RuntimeError(f"unsupported tenant bot role: {spec.role}")
