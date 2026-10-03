"""Payment-provider boundary for TenantRuntime.

UserBot and AdminBot consume provider metadata through this registry instead of
branching on card/crypto implementation details. New providers can register a
spec and reuse the existing checkout/receipt UI without changing UserBot.

The legacy tenant_payment_methods.kind column is intentionally kept as a
storage compatibility family (card/crypto). provider_key is the canonical
runtime identity for providers added from v0.30.0 onward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaymentProviderSpec:
    key: str
    title: str
    icon: str
    legacy_kind: str
    destination_label: str
    requires_network: bool = False
    requires_receipt: bool = True
    manual_review: bool = True
    user_selectable: bool = True

    def __post_init__(self) -> None:
        if not self.key or ":" in self.key:
            raise ValueError("invalid payment provider key")
        if self.legacy_kind not in ("card", "crypto"):
            raise ValueError("legacy payment family must be card or crypto")


_REGISTRY: dict[str, PaymentProviderSpec] = {}


def register_provider(spec: PaymentProviderSpec, *, replace: bool = False) -> None:
    current = _REGISTRY.get(spec.key)
    if current is not None and not replace:
        raise ValueError(f"payment provider already registered: {spec.key}")
    _REGISTRY[spec.key] = spec


def registered_providers(*, user_selectable: bool | None = None) -> list[PaymentProviderSpec]:
    items = list(_REGISTRY.values())
    if user_selectable is not None:
        items = [item for item in items if item.user_selectable is user_selectable]
    return sorted(items, key=lambda item: (item.title, item.key))


def provider_for_key(key: object, *, legacy_kind: object = "") -> PaymentProviderSpec:
    clean = str(key or "").strip().lower()
    if clean in _REGISTRY:
        return _REGISTRY[clean]
    legacy = str(legacy_kind or "").strip().lower()
    fallback_key = "card_manual" if legacy == "card" else "crypto_manual"
    if not clean:
        return _REGISTRY[fallback_key]
    base = _REGISTRY[fallback_key]
    return PaymentProviderSpec(
        key=clean,
        title=clean.replace("_", " ").title(),
        icon="💳" if legacy == "card" else "🔗",
        legacy_kind=base.legacy_kind,
        destination_label=base.destination_label,
        requires_network=legacy == "crypto",
        requires_receipt=True,
        manual_review=True,
        user_selectable=False,
    )


def provider_for_method(method: dict[str, Any]) -> PaymentProviderSpec:
    return provider_for_key(
        method.get("provider_key"),
        legacy_kind=method.get("kind"),
    )


def method_view(method: dict[str, Any]) -> dict[str, Any]:
    item = dict(method)
    provider = provider_for_method(item)
    item["provider_key"] = provider.key
    item["provider_title"] = provider.title
    item["provider_icon"] = provider.icon
    item["destination_label"] = provider.destination_label
    item["requires_network"] = provider.requires_network
    item["requires_receipt"] = provider.requires_receipt
    item["manual_review"] = provider.manual_review
    return item


def payment_prompt(method: dict[str, Any], *, amount: int | None = None) -> str:
    item = method_view(method)
    lines = [f"{item['provider_icon']} {item['provider_title']}"]
    if amount is not None:
        lines.append(f"💰 مبلغ: {int(amount):,} {item.get('currency') or ''}")
    if item.get("network"):
        lines.append(f"🌐 شبکه: {item['network']}")
    lines.append(f"{item['destination_label']}: {item.get('destination') or '-'}")
    instructions = str(item.get("instructions") or "").strip()
    if instructions:
        lines.extend(["", instructions])
    if bool(item.get("requires_receipt", True)):
        lines.extend(["", "🧾 کد پیگیری یا تصویر رسید را ارسال کنید."])
    return "\n".join(lines)


register_provider(PaymentProviderSpec(
    key="card_manual",
    title="کارت به کارت",
    icon="💳",
    legacy_kind="card",
    destination_label="شماره کارت / مقصد",
))
register_provider(PaymentProviderSpec(
    key="crypto_manual",
    title="ارز دیجیتال",
    icon="🪙",
    legacy_kind="crypto",
    destination_label="آدرس کیف پول",
    requires_network=True,
))
