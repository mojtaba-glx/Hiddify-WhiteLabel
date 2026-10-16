"""Regression tests verifying UserBot panel-touching operations are offloaded off the event loop."""

import asyncio
import functools
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from TenantRuntime.UserBot import handlers
from TenantRuntime.business import TenantBusinessError
from Tests.test_user_account_status_phase6 import _service


class Messages:
    def __init__(self):
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.text = ""
        self.photos: list[tuple[bytes, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.sent.append((text, kwargs))

    async def reply_photo(self, photo: Any, **kwargs: Any) -> None:
        self.photos.append((photo.getvalue(), kwargs))


class Query:
    def __init__(self, data: str, message: Messages):
        self.data = data
        self.message = message
        self.answers: list[tuple[Any, Any]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.message.sent.append((text, kwargs))


def _setup_session(monkeypatch, business, actor: int):
    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=actor),
        effective_message=message,
        callback_query=None,
    )

    class Policy:
        def check(self, spec, telegram_user_id):
            return SimpleNamespace(allowed=True, reason="")

    class StateStore:
        def load(self, user_id):
            return {}

        def save(self, user_id, state):
            self.state = state

    monkeypatch.setattr(
        handlers,
        "_services",
        lambda _: (
            SimpleNamespace(role="user"),
            Policy(),
            StateStore(),
            business,
        ),
    )

    async def _allowed(*_args):
        return True

    monkeypatch.setattr(handlers, "_force_join_allowed", _allowed)
    return update, context, message


async def _click(update: SimpleNamespace, context: SimpleNamespace, data: str) -> Query:
    update.callback_query = Query(data, update.effective_message)
    await handlers.on_callback(update, context)
    return update.callback_query


def test_show_status_offloaded_via_to_thread(monkeypatch, conn, factories, cipher):
    """Prove customer_subscription_status in show_status runs off the event-loop thread."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = _setup_session(monkeypatch, service, actor)

    loop_thread_id = threading.get_ident()
    observed_threads: list[int] = []
    to_thread_calls: list[str] = []

    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

    orig_status = service.customer_subscription_status

    @functools.wraps(orig_status)
    def instrumented_status(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        return orig_status(*args, **kwargs)

    monkeypatch.setattr(service, "customer_subscription_status", instrumented_status)

    asyncio.run(handlers.show_status(update, context))

    assert len(to_thread_calls) >= 1
    assert any("_fetch_direct_items" in name for name in to_thread_calls)
    assert len(observed_threads) == 1
    assert observed_threads[0] != loop_thread_id, "Sync panel query executed on the event-loop thread!"

    assert len(message.sent) == 1
    text, _ = message.sent[0]
    assert text.startswith("📄اطلاعات اشتراک شما\n")
    assert "👤نام:" in text


def test_show_status_event_loop_responsiveness_during_slow_panel(
    monkeypatch, conn, factories, cipher
):
    """Prove slow sync panel I/O does not block the asyncio event loop."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = _setup_session(monkeypatch, service, actor)

    orig_status = service.customer_subscription_status

    def slow_status(*args, **kwargs):
        time.sleep(0.12)  # Blocking sleep simulating remote panel latency
        return orig_status(*args, **kwargs)

    monkeypatch.setattr(service, "customer_subscription_status", slow_status)

    heartbeat_ticks = 0

    async def heartbeat():
        nonlocal heartbeat_ticks
        for _ in range(15):
            await asyncio.sleep(0.015)
            heartbeat_ticks += 1

    async def runner():
        await asyncio.gather(
            handlers.show_status(update, context),
            heartbeat(),
        )

    asyncio.run(runner())

    # If blocking panel I/O ran on loop, heartbeat couldn't tick until slow_status finished.
    assert heartbeat_ticks >= 3, f"Event loop was starved! Heartbeat ticks: {heartbeat_ticks}"
    assert len(message.sent) == 1
    assert message.sent[0][0].startswith("📄اطلاعات اشتراک شما\n")


def test_show_status_exception_semantics_preserved(
    monkeypatch, conn, factories, cipher
):
    """Prove TenantBusinessError from offloaded call falls back to standard retry text."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = _setup_session(monkeypatch, service, actor)

    def failing_status(*args, **kwargs):
        raise TenantBusinessError("panel timeout")

    monkeypatch.setattr(service, "customer_subscription_status", failing_status)

    asyncio.run(handlers.show_status(update, context))

    assert len(message.sent) == 1
    text, kwargs = message.sent[0]
    assert text == "⏳ دریافت وضعیت اشتراک‌ها موقتاً ممکن نیست."
    buttons = [
        btn.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert buttons == ["runtime:home"]


def test_shop_account_offloaded_via_to_thread(
    monkeypatch, conn, factories, cipher
):
    """Prove shop:account callback offloads customer_account_summary(refresh=True)."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = _setup_session(monkeypatch, service, actor)

    loop_thread_id = threading.get_ident()
    observed_threads: list[int] = []
    to_thread_calls: list[str] = []

    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

    orig_summary = service.customer_account_summary

    @functools.wraps(orig_summary)
    def instrumented_summary(*args, **kwargs):
        observed_threads.append(threading.get_ident())
        assert kwargs.get("refresh") is True
        return orig_summary(*args, **kwargs)

    monkeypatch.setattr(service, "customer_account_summary", instrumented_summary)

    async def runner():
        await _click(update, context, "shop:account")

    asyncio.run(runner())

    assert any("customer_account_summary" in name for name in to_thread_calls)
    assert len(observed_threads) == 1
    assert observed_threads[0] != loop_thread_id, "Summary executed on the event-loop thread!"

    assert len(message.sent) == 1
    text, kwargs = message.sent[0]
    assert "👤 پروفایل کاربر" in text and "Telegram ID: 7101" in text
    buttons = [
        btn.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert "shop:subs:active" in buttons
    assert "shop:subs:expired" in buttons
    assert "runtime:home" in buttons


def test_substatus_and_subrefresh_offloaded_via_to_thread(
    monkeypatch, conn, factories, cipher
):
    """Prove shop:subrefresh / shop:substatus offload status and visibility checks."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = _setup_session(monkeypatch, service, actor)

    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

    async def runner():
        await _click(update, context, f"shop:subrefresh:{sid}")

    asyncio.run(runner())

    assert any("customer_subscription_status" in name for name in to_thread_calls)
    assert any("_single_subscription_visibility" in name for name in to_thread_calls)
    assert len(message.sent) >= 1
    detail = message.sent[-1][0]
    assert detail.startswith("📄اطلاعات اشتراک شما")


def test_subscription_configs_offloaded_via_to_thread(
    monkeypatch, conn, factories, cipher
):
    """Prove shop:configs offloads business.subscription_configs."""
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    panel.subscription_content = lambda *args, **kwargs: "vless://test@127.0.0.1:443"
    update, context, message = _setup_session(monkeypatch, service, actor)

    to_thread_calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracking_to_thread)

    async def runner():
        await _click(update, context, f"shop:configs:{sid}")

    asyncio.run(runner())

    assert any("subscription_configs" in name for name in to_thread_calls)
    assert len(message.sent) >= 1
    text = message.sent[-1][0]
    assert "کانفیگ‌های مستقیم" in text
