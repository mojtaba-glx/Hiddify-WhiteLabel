import json
import sqlite3
import pytest
from MasterBot.customer_service import CustomerPortalService
from MasterBot.service import MasterService
from TenantRuntime import sms_webhook
from MasterBot import customer_views


@pytest.fixture
def portal(conn, cipher):
    master = MasterService(conn, master_admin_id=123, cipher=cipher)
    return CustomerPortalService(conn, master_service=master)


def test_platform_settings_payment_keys(portal):
    settings = portal.storefront_settings()
    assert "require_last4_for_card_receipt" in settings
    assert "random_tx_spec" in settings
    assert "card_to_card_text" in settings
    assert settings["require_last4_for_card_receipt"] is False

    portal.settings.set_bool("require_last4_for_card_receipt", True)
    assert portal.storefront_settings()["require_last4_for_card_receipt"] is True

    portal.settings.set_text("card_to_card_text", "کارت: {CARD} | بانک: {BANK}", maximum=500)
    assert portal.storefront_settings()["card_to_card_text"] == "کارت: {CARD} | بانک: {BANK}"


def test_payment_method_card_and_crypto_lifecycle(portal):
    actor = 123

    # 1. Add Card
    card = portal.add_payment_method(
        actor,
        kind="card",
        title="کارت سامان ۱۲۳۴",
        currency="TOMAN",
        destination="6037991234561234 | علی محمدی | سامان",
        recipient="علی محمدی",
        network="سامان",
    )
    assert card["kind"] == "card"
    assert card["currency"] == "TOMAN"
    assert card["recipient"] == "علی محمدی"
    assert card["network"] == "سامان"

    # 2. Add Crypto
    crypto = portal.add_payment_method(
        actor,
        kind="crypto",
        title="تتر ترون",
        currency="USDT",
        destination="TXYZ1234567890",
        network="TRC20",
        instructions="واریز فقط از ترون",
    )
    assert crypto["kind"] == "crypto"
    assert crypto["network"] == "TRC20"

    # 3. Edit card
    updated_card = portal.update_payment_method(
        actor,
        int(card["id"]),
        title="کارت ملی ۱۲۳۴",
        currency="TOMAN",
        destination="6037991234561234 | علی رضایی | ملی",
        recipient="علی رضایی",
        network="ملی",
    )
    assert updated_card["recipient"] == "علی رضایی"
    assert updated_card["network"] == "ملی"

    # 4. Safe remove / delete
    res = portal.remove_payment_method_admin(actor, method_id=int(crypto["id"]))
    assert res["removed"] is True
    assert portal.conn.execute("SELECT * FROM payment_methods WHERE id=?", (int(crypto["id"]),)).fetchone() is None


def test_sms_settings_and_rotation(portal):
    actor = 123

    status = portal.sms_status_admin(actor)
    assert status["enabled"] is False
    assert status["webhook_path"] == "/payment/sms/master"

    # Enable generates secret if none
    status = portal.set_sms_enabled_admin(actor, enabled=True)
    assert status["enabled"] is True
    assert status["has_secret"] is True
    assert bool(status["masked_secret"])

    # Rotate secret
    rotated = portal.rotate_sms_secret_admin(actor)
    plain_secret = rotated["plain_secret"]
    assert len(plain_secret) == 64
    assert portal.sms_secret_matches(plain_secret) is True
    assert portal.sms_secret_matches("wrong") is False


def test_master_sms_webhook_flow(portal, db_path):
    actor = 123

    # Enable SMS and get secret
    portal.set_sms_enabled_admin(actor, enabled=True)
    secret = portal.sms_plain_secret_admin(actor)

    # Register customer and create order
    customer = portal.register_customer(999, display_name="Test Customer")
    order = portal.create_wallet_topup(999, currency="TOMAN", amount=250000)

    card = portal.add_payment_method(
        actor,
        kind="card",
        title="کارت سامان",
        currency="TOMAN",
        destination="6037991234561234",
        recipient="مدیر سیستم",
        network="سامان",
    )

    # Submit receipt
    receipt = portal.submit_receipt(
        999,
        order_id=int(order["id"]),
        payment_method_id=int(card["id"]),
        reference="ref123 | 1234",
    )

    # 1. Webhook auth failure
    code, res = sms_webhook.handle_sms_post(
        db_path,
        "master",
        {"X-WhiteLabel-Sms-Secret": "invalid"},
        b"{}",
        cipher=None,
    )
    assert code == 401
    assert res["ok"] is False

    # 2. Test payload
    code, res = sms_webhook.handle_sms_post(
        db_path,
        "master",
        {"X-WhiteLabel-Sms-Secret": secret},
        json.dumps({"event_id": "test-ev-1", "test": True}).encode(),
        cipher=None,
    )
    assert code == 200
    assert res["ok"] is True
    assert res["test"] is True

    # 3. Valid bank SMS approving order
    payload = {
        "event_id": "sms-tx-100",
        "amount": 2500000,
        "currency": "rial",
        "reference": "ref999",
        "sender": "20004861",
        "card_last4": "1234",
        "body": "واریز ۲,۵۰۰,۰۰۰ ریال به کارت ۱۲۳۴",
    }
    code, res = sms_webhook.handle_sms_post(
        db_path,
        "master",
        {"X-WhiteLabel-Sms-Secret": secret},
        json.dumps(payload).encode(),
        cipher=None,
    )
    assert code == 200
    assert res["ok"] is True
    assert res["status"] == "approved"
    assert res["receipt_id"] == int(receipt["id"])

    # Verify order is fulfilled and wallet credited
    updated_order = portal.get_order(999, int(order["id"]))
    assert updated_order["status"] == "fulfilled"
    assert portal.wallet_balance(999, "TOMAN") == 250000

    # 4. Duplicate SMS post
    code, res = sms_webhook.handle_sms_post(
        db_path,
        "master",
        {"X-WhiteLabel-Sms-Secret": secret},
        json.dumps(payload).encode(),
        cipher=None,
    )
    assert code == 200
    assert res["duplicate"] is True

    # 5. Anti-cross-user replay (late SMS attaches to manual approval)
    late_payload = {
        "event_id": "sms-tx-200",
        "amount": 250000,
        "currency": "toman",
        "reference": "ref999",
        "sender": "20004861",
        "card_last4": "",
        "body": "واریز ۲۵۰,۰۰۰ تومان",
    }
    code, res = sms_webhook.handle_sms_post(
        db_path,
        "master",
        {"X-WhiteLabel-Sms-Secret": secret},
        json.dumps(late_payload).encode(),
        cipher=None,
    )
    assert code == 200
    assert res["status"] == "attached_manual"


def test_customer_view_card_rendering():
    method = {
        "kind": "card",
        "title": "کارت سامان ۱۲۳۴",
        "destination": "6037991234561234 | علی محمدی | سامان",
        "recipient": "علی محمدی",
        "network": "سامان",
        "instructions": "",
    }
    order = {
        "public_id": "ord_123",
        "amount": 100000,
        "currency": "TOMAN",
    }
    settings = {
        "require_last4_for_card_receipt": True,
        "card_to_card_text": "واریز به {BANK} شماره {CARD} به نام {OWNER} مبلغ {AMOUNT}",
    }

    text = customer_views.payment_instructions(method, order, settings=settings)
    assert "سامان" in text
    assert "6037991234561234" in text
    assert "علی محمدی" in text
    assert "100,000 تومان" in text
    assert "۴ رقم آخر شماره کارت" in text

def test_master_handlers_card_and_crypto_flows(conn, cipher):
    import asyncio

    async def _run():
        from MasterBot import handlers
        from unittest.mock import AsyncMock
        from types import SimpleNamespace

        actor_id = 123
        master = MasterService(conn, master_admin_id=actor_id, cipher=cipher)
        portal = CustomerPortalService(conn, master_service=master)

        context = SimpleNamespace(
            bot_data={
                "customer_portal_service": portal,
                "master_service": master,
            },
            user_data={},
        )

        # 1. Callback payment:card opens card menu
        query = AsyncMock()
        query.data = "payment:card"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=actor_id), effective_message=None)
        await handlers.on_callback(update, context)
        assert query.edit_message_text.called
        _, kwargs = query.edit_message_text.call_args
        assert "💳 تنظیمات کارت به کارت" in kwargs["text"]

        # 2. Add card step 1: payment:card:add
        query = AsyncMock()
        query.data = "payment:card:add"
        query.answer = AsyncMock()
        eff_msg = AsyncMock()
        eff_msg.reply_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=actor_id), effective_message=eff_msg)
        await handlers.on_callback(update, context)
        assert context.user_data["flow"]["kind"] == "payment_card_bank"

        # Step 1 input: Bank name
        msg = AsyncMock()
        msg.text = "ملی"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_card_number"
        assert context.user_data["flow"]["bank"] == "ملی"

        # Step 2 input: Card number (16 digits with Persian digits and dashes)
        msg = AsyncMock()
        msg.text = "۶۰۳۷-۹۹۱۸-۱۲۳۴-۵۶۷۸"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_card_owner"
        assert context.user_data["flow"]["number"] == "6037991812345678"

        # Step 3 input: Owner name
        msg = AsyncMock()
        msg.text = "علی احمدی"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_card_currency"
        assert context.user_data["flow"]["owner"] == "علی احمدی"

        # Step 4 input: Currency (0 for default TOMAN)
        msg = AsyncMock()
        msg.text = "0"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert "flow" not in context.user_data

        # Check that method was added
        cards = [m for m in portal.list_all_payment_methods(actor_id) if m["kind"] == "card"]
        assert len(cards) == 1
        assert cards[0]["destination"].startswith("6037991812345678")
        assert cards[0]["recipient"] == "علی احمدی"
        assert cards[0]["network"] == "ملی"
        assert cards[0]["currency"] == "TOMAN"

        # 3. Add Crypto step by step
        query = AsyncMock()
        query.data = "payment:crypto:add"
        query.answer = AsyncMock()
        eff_msg = AsyncMock()
        eff_msg.reply_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=actor_id), effective_message=eff_msg)
        await handlers.on_callback(update, context)
        assert context.user_data["flow"]["kind"] == "payment_crypto_title"

        # Crypto step 1: title
        msg = AsyncMock()
        msg.text = "تتر ترون"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_crypto_currency"

        # Crypto step 2: currency
        msg = AsyncMock()
        msg.text = "USDT"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_crypto_destination"

        # Crypto step 3: destination
        msg = AsyncMock()
        msg.text = "TYu123456789"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_crypto_network"

        # Crypto step 4: network
        msg = AsyncMock()
        msg.text = "TRC20"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert context.user_data["flow"]["kind"] == "payment_crypto_instructions"

        # Crypto step 5: instructions
        msg = AsyncMock()
        msg.text = "واریز فقط ترون"
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=actor_id), effective_message=msg, effective_chat=msg, callback_query=None)
        await handlers.on_text(update, context)
        assert "flow" not in context.user_data

        cryptos = [m for m in portal.list_all_payment_methods(actor_id) if m["kind"] == "crypto"]
        assert len(cryptos) == 1
        assert cryptos[0]["title"] == "تتر ترون"
        assert cryptos[0]["destination"] == "TYu123456789"
        assert cryptos[0]["network"] == "TRC20"