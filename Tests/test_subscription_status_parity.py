"""Customer subscription menus, ownership and resumable panel identity changes."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from Shared.timeutils import iso_utc, utcnow
from TenantRuntime.UserBot import handlers
from TenantRuntime.business import TenantBusinessError
from TenantRuntime.panels import PanelError, PanelUserResult, PanelTarget
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.xui import XuiPanelAdapter
from TenantRuntime.xnet import XnetPanelAdapter
from Tests.test_user_account_status_phase6 import _service


class Messages:
    def __init__(self):
        self.sent = []
        self.text = ""
        self.photos = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def reply_photo(self, photo, **kwargs):
        self.photos.append((photo.getvalue(), kwargs))


class Query:
    def __init__(self, data, message):
        self.data, self.message = data, message
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.message.sent.append((text, kwargs))


def session(monkeypatch, business, actor):
    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(effective_user=SimpleNamespace(id=actor),
        effective_message=message, callback_query=None)
    monkeypatch.setattr(handlers, '_services', lambda _: (SimpleNamespace(role='user'), None, None, business))
    async def allowed(*args):
        return True
    monkeypatch.setattr(handlers, '_force_join_allowed', allowed)
    return update, context, message


async def click(update, context, data):
    update.callback_query = Query(data, update.effective_message)
    await handlers.on_callback(update, context)
    return update.callback_query


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]



def test_single_subscription_status_opens_four_actions_directly(
    monkeypatch, conn, factories, cipher
):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)

    class Policy:
        def check(self, spec, telegram_user_id):
            return SimpleNamespace(allowed=True, reason='')

    class StateStore:
        def load(self, user_id):
            return {}

        def save(self, user_id, state):
            self.state = state

    message = Messages()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=actor),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(
        handlers,
        '_services',
        lambda _: (
            SimpleNamespace(role='user'),
            Policy(),
            StateStore(),
            service,
        ),
    )

    async def allowed(*args):
        return True

    monkeypatch.setattr(handlers, '_force_join_allowed', allowed)
    asyncio.run(handlers.show_status(update, context))

    assert len(message.sent) == 1
    text, kwargs = message.sent[0]
    assert text == '📊 وضعیت اشتراک\n\nیکی از گزینه‌های زیر را انتخاب کنید:'
    assert callbacks(kwargs['reply_markup']) == [
        f'shop:configmenu:{sid}',
        f'shop:renew:{sid}',
        f'shop:subrename:{sid}',
        f'shop:subrotate:{sid}',
    ]
    for removed in (
        'فعال', 'منقضی', 'غیرفعال', 'در انتظار',
        'پروفایل', 'سفارش‌های من', 'همه اشتراک‌ها',
    ):
        assert removed not in text



def attach_rotation(service, panel, *, fail_after=None, reset=False):
    panel.calls = []
    def rotate(**kwargs):
        panel.calls.append(kwargs)
        if fail_after is not None and len(panel.calls) == fail_after:
            raise PanelError('offline')
        if reset:
            panel.usage_bytes = 0
        return PanelUserResult(kwargs['new_ref'], panel.usage_bytes, panel.active)
    panel.rotate_identity = rotate


def test_status_menu_hides_detail_fields_and_rename_still_works(conn, factories, cipher):
    service, panel, actor, sid, oid = _service(conn, factories, cipher)
    item = service.rename_customer_subscription(actor, subscription_id=sid, name='اشتراک <جدید>')
    text = handlers._subscription_menu_text()
    assert text == '📊 وضعیت اشتراک\n\nیکی از گزینه‌های زیر را انتخاب کنید:'
    for hidden in (
        '✏️ نام', '👤 کاربر', '📦 پلن', '📡 سرور', '📶 وضعیت',
        '📊 میزان استفاده', '📥 حجم باقی‌مانده', '📅 تاریخ انقضا',
        '⏳ زمان باقی‌مانده', '🕓 آخرین اتصال', '💰 قیمت اشتراک',
        '🔑 شناسه', '🔄 آخرین بروزرسانی',
    ):
        assert hidden not in text
    assert service.customer_subscription_status(
        actor, subscription_id=sid, refresh=False
    )['service_name'] == 'اشتراک <جدید>'
    for invalid in ('xx', 'a' * 65, 'نام\nجدید', 'نام\x00جدید'):
        with pytest.raises(ValueError):
            service.rename_customer_subscription(actor, subscription_id=sid, name=invalid)


def test_customer_mutations_cannot_cross_customer_or_tenant(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    attach_rotation(service, panel)
    other = actor + 1
    service.register_customer(other, display_name='Other', username='other')
    for method, args in ((service.rename_customer_subscription, {'name': 'New name'}),
                         (service.rotate_customer_subscription, {})):
        with pytest.raises(TenantBusinessError):
            method(other, subscription_id=sid, **args)
    assert not panel.calls
    service.tenant_id += 1000
    with pytest.raises((TenantBusinessError, PermissionError)):
        service.rotate_customer_subscription(actor, subscription_id=sid)


def test_large_subscription_list_is_sellbot_style_only(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    customer = service._customer(actor)
    item = service.customer_subscription_status(
        actor, subscription_id=sid, refresh=False
    )
    for _ in range(5):
        order = service.create_order(
            actor,
            item['plan_id'],
            server_id=item['server_id'],
        )
        conn.execute(
            "INSERT INTO tenant_subscriptions "
            "(tenant_id,customer_id,plan_id,order_id,status,traffic_bytes,"
            "expires_at,created_at,updated_at) "
            "VALUES (?,?,?,?,'pending_provisioning',?,?,?,?)",
            (
                service.tenant_id,
                customer['id'],
                item['plan_id'],
                order['id'],
                1024,
                item['expires_at'],
                iso_utc(utcnow()),
                iso_utc(utcnow()),
            ),
        )
    conn.commit()

    text, markup = handlers._subscription_list(service, actor, {})
    data = callbacks(markup)
    assert text == '👇 لطفا یکی از اشتراک‌های خود را انتخاب نمایید'
    assert sum(x.startswith('shop:substatus:') for x in data) == 6
    assert data[-1] == 'runtime:home'
    for removed in (
        'shop:subs:active',
        'shop:subs:expired',
        'shop:subs:disabled',
        'shop:subs:pending_provisioning',
        'shop:account',
        'shop:orders',
    ):
        assert removed not in data


def test_rotation_preserves_terms_and_invalidates_smart_link(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    attach_rotation(service, panel, reset=True)
    before = service.customer_subscription_status(actor, subscription_id=sid, refresh=True)
    now = iso_utc(utcnow())
    conn.execute("INSERT INTO tenant_smart_links (tenant_id,code,label,target,status,created_at,updated_at) VALUES (?,'old-code','Service',?,'active',?,?)", (service.tenant_id, f'subscription:{sid}', now, now))
    conn.commit()
    after = service.rotate_customer_subscription(actor, subscription_id=sid)
    assert after['external_ref'] != before['external_ref']
    for key in ('expires_at', 'traffic_bytes', 'usage_bytes', 'status'):
        assert after[key] == before[key]
    refreshed = service.customer_subscription_status(actor, subscription_id=sid, refresh=True)
    assert refreshed['usage_bytes'] == before['usage_bytes']
    assert conn.execute('SELECT code FROM tenant_smart_links').fetchone()[0] != 'old-code'
    assert not after['rotation_pending']


def test_rotation_resumes_nodes_after_failure_without_rotating_primary_again(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    node = service.add_server(7001, label='Node', panel_kind='hiddify', endpoint='https://node.example', user_path='user')
    service.set_panel_credential(7001, server_id=node['id'], secret='node-key')
    item = service.customer_subscription_status(actor, subscription_id=sid, refresh=False)
    service._ensure_primary_subscription_node(item)
    service._upsert_subscription_node(subscription_id=sid, server_id=node['id'], external_ref='node-old', is_primary=False, status='active', usage_bytes=1024)
    conn.commit()
    attach_rotation(service, panel, fail_after=2)
    with pytest.raises(TenantBusinessError, match='pending'):
        service.rotate_customer_subscription(actor, subscription_id=sid)
    partial = service.customer_subscription_status(actor, subscription_id=sid, refresh=False)
    desired = partial['external_ref']
    assert partial['rotation_pending']
    for method in (service.subscription_link, service.subscription_configs, service.automatic_subscription_link):
        with pytest.raises(TenantBusinessError, match='pending'):
            method(actor, subscription_id=sid)
    with pytest.raises(TenantBusinessError, match='pending'):
        service.repair_subscription_nodes(7001, subscription_id=sid)
    final = service.rotate_customer_subscription(actor, subscription_id=sid)
    assert final['external_ref'] == desired and not final['rotation_pending']
    assert len(panel.calls) == 3
    assert panel.calls[0]['new_ref'] == panel.calls[1]['new_ref'] == panel.calls[2]['new_ref']
    assert panel.calls[-1]['external_ref'] == 'node-old'


def test_customer_buttons_rename_cancel_and_rotation_confirm_once(monkeypatch, conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    attach_rotation(service, panel)
    update, context, message = session(monkeypatch, service, actor)
    async def run():
        await click(update, context, f'shop:substatus:{sid}')
        data = callbacks(message.sent[-1][1]['reply_markup'])
        assert data == [
            f'shop:configmenu:{sid}',
            f'shop:renew:{sid}',
            f'shop:subrename:{sid}',
            f'shop:subrotate:{sid}',
        ]
        assert message.sent[-1][0] == '📊 وضعیت اشتراک\n\nیکی از گزینه‌های زیر را انتخاب کنید:'
        await click(update, context, f'shop:subrename:{sid}')
        update.callback_query = None
        message.text = 'نام جدید'
        await handlers.unknown_text(update, context)
        assert message.sent[-1][0].startswith('✅ نام اشتراک تغییر کرد.')
        assert service.customer_subscription_status(
            actor, subscription_id=sid, refresh=False
        )['service_name'] == 'نام جدید'
        await click(update, context, f'shop:subrename:{sid}')
        await click(update, context, f'shop:subrefresh:{sid}')
        assert 'biz_flow' not in context.user_data
        # A callback without a confirmation cannot mutate credentials.
        await click(update, context, f'shop:subrotateconfirm:{sid}')
        assert not panel.calls
        await click(update, context, f'shop:subrotate:{sid}')
        assert 'از کار می‌افتد' in message.sent[-1][0]
        await click(update, context, f'shop:subrotateconfirm:{sid}')
        assert len(panel.calls) == 1
        await click(update, context, f'shop:subrotateconfirm:{sid}')
        assert len(panel.calls) == 1
    asyncio.run(run())


def test_qr_link_is_copyable_html(monkeypatch, conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    update, context, message = session(monkeypatch, service, actor)
    asyncio.run(click(update, context, f'shop:sublink:{sid}'))
    assert message.photos
    data, kwargs = message.photos[-1]
    assert data.startswith(b'\x89PNG')
    assert '<code>' in kwargs['caption'] and kwargs['parse_mode'] == 'HTML'


def test_hiddify_rotation_retries_lost_response_without_reset():
    old, new = 'old-user', 'new-user'
    users = {old: {'uuid': old, 'current_usage_GB': 3, 'usage_limit_GB': 10, 'is_active': True}}
    bodies = []
    def handler(request):
        if '/panel/info/' in request.url.path:
            return httpx.Response(200, json={'version': '13'}, request=request)
        ref = request.url.path.rstrip('/').split('/')[-1]
        if request.method == 'PATCH':
            body = json.loads(request.content)
            bodies.append(body)
            users[new] = users.pop(old) | body
            return httpx.Response(200, json=users[new], request=request)
        return httpx.Response(200 if ref in users else 404, json=users.get(ref, {}), request=request)
    adapter = HiddifyPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget('hiddify', 'https://panel.example', admin_path='admin', user_path='user')
    first = adapter.rotate_identity(target=target, secret='secret', external_ref=old, new_ref=new)
    again = adapter.rotate_identity(target=target, secret='secret', external_ref=old, new_ref=new)
    assert bodies == [{'uuid': new}]
    assert first == again and first.usage_bytes == 3 * 1024**3


@pytest.mark.parametrize('flavor', ['sanaei', 'alireza'])
def test_xui_rotation_preserves_traffic_identity_and_retries_inbounds(flavor):
    old, new = 'old-user', 'new-user'
    clients = [dict(id=old, uuid=old, subId=old, email=f'user-{i}', password=old,
                    totalGB=12345, expiryTime=1893456000000, enable=False, limitIp=2,
                    comment='renew-marker', traffic={'up': 100, 'down': 200}) for i in range(2)]
    inbound = lambda: [dict(id=i+1, protocol='vless', settings=json.dumps({'clients': [c]}),
                            clientStats=[{'email': c['email'], 'up': 100, 'down': 200}]) for i, c in enumerate(clients)]
    modern = flavor == 'sanaei'
    requests = []
    class Session:
        modern_clients = modern
        target = PanelTarget('xui', 'https://panel.example', xui_flavor=flavor)
        def request(self, method, path, *, payload=None, **kwargs):
            requests.append((method, path, payload))
            assert 'reset' not in path
            if modern:
                client = next(c for c in clients if c['email'] == path.rsplit('/', 1)[1])
                client.update(payload)
            else:
                index = payload['id'] - 1
                clients[index].update(json.loads(payload['settings'])['clients'][0])
    adapter = XuiPanelAdapter()
    @contextmanager
    def session(*args):
        yield Session()
    adapter._session = session
    adapter._list_inbounds = lambda _: inbound()
    adapter._list_sanaei_clients = lambda _: clients
    adapter._online_set = lambda _: set()
    adapter._last_online_map = lambda _: {}
    if modern:
        clients.pop()  # Modern first-class client represents all inbound memberships.
    result = adapter.rotate_identity(target=Session.target, secret='secret', external_ref=old, new_ref=new)
    count = len(requests)
    adapter.rotate_identity(target=Session.target, secret='secret', external_ref=old, new_ref=new)
    assert len(requests) == count
    assert result.external_ref == new
    for i, client in enumerate(clients):
        assert client['email'] == f'user-{i}'
        assert client['totalGB'] == 12345 and client['expiryTime'] == 1893456000000
        assert client['enable'] is False and client['password'] == new
        assert client['comment'] == 'renew-marker'
        assert client['traffic'] == {'up': 100, 'down': 200}


def test_xnet_rotation_preserves_internal_id_and_all_inbound_usage():
    old, new = 'old-user', 'new-user'
    rows = [dict(id=f'in-{i}', protocol='VLESS', clients=[dict(id=f'client-{i}', uuid=old,
            username=f'name-{i}', password=old, status='disabled', trafficLimitBytes=12345,
            trafficUsedBytes=100, expireDate='2030-01-01T00:00:00Z')]) for i in range(2)]
    calls = []
    class Session:
        target = PanelTarget('xnet', 'https://panel.example')
        def request(self, method, path, *, payload=None, **kwargs):
            if method == 'PUT':
                calls.append(path)
            assert 'reset' not in path
            for row in rows:
                for client in row['clients']:
                    if method == 'PUT' and path.endswith('/' + client['id']):
                        client.update(payload)
    adapter = XnetPanelAdapter()
    @contextmanager
    def session(*args):
        yield Session()
    adapter._session = session
    adapter._inbounds = lambda _: rows
    adapter._online_map = lambda _: {}
    result = adapter.rotate_identity(target=Session.target, secret='secret', external_ref=old, new_ref=new)
    adapter.rotate_identity(target=Session.target, secret='secret', external_ref=old, new_ref=new)
    assert len(calls) == 2 and result.external_ref == new
    for index, row in enumerate(rows):
        c = row['clients'][0]
        assert c['id'] == f'client-{index}' and c['username'] == f'name-{index}'
        assert c['trafficUsedBytes'] == 100 and c['trafficLimitBytes'] == 12345
        assert c['expireDate'] == '2030-01-01T00:00:00Z'


def test_rotation_preflight_failure_keeps_links_and_does_not_block_service(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    attach_rotation(service, panel)
    def unsupported(**kwargs):
        raise PanelError('unsupported protocol')
    panel.validate_identity_rotation = unsupported
    old = service.subscription_link(actor, subscription_id=sid)
    with pytest.raises(TenantBusinessError, match='unavailable'):
        service.rotate_customer_subscription(actor, subscription_id=sid)
    assert not panel.calls
    assert not service.customer_subscription_status(actor, subscription_id=sid, refresh=False)['rotation_pending']
    assert service.subscription_link(actor, subscription_id=sid) == old


def test_offset_survives_disable_and_expiry_and_resets_on_renewal(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    attach_rotation(service, panel, reset=True)
    service.rotate_customer_subscription(actor, subscription_id=sid)
    service.set_subscription_enabled(7001, subscription_id=sid, enabled=False)
    assert conn.execute('SELECT usage_bytes FROM tenant_subscription_nodes').fetchone()[0] == 3 * 1024**3
    service.set_subscription_enabled(7001, subscription_id=sid, enabled=True)
    conn.execute("UPDATE tenant_subscriptions SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (sid,))
    conn.commit()
    service.expire_subscription(7001, subscription_id=sid)
    assert conn.execute('SELECT usage_bytes FROM tenant_subscription_nodes').fetchone()[0] == 3 * 1024**3
    def renew(**kwargs):
        panel.active = True
        panel.usage_bytes = 0
        return PanelUserResult(kwargs['external_ref'], 0, True,
                               traffic_bytes=kwargs['request'].traffic_bytes,
                               expires_at=kwargs['request'].expires_at)
    panel.renew = renew
    service.renew_subscription(7001, subscription_id=sid, traffic_gb=10, duration_days=30)
    row = conn.execute('SELECT usage_bytes,usage_offset_bytes FROM tenant_subscription_nodes').fetchone()
    assert tuple(row) == (0, 0)
    assert service.customer_subscription_status(actor, subscription_id=sid, refresh=True)['usage_bytes'] == 0


def test_unavailable_panel_marks_cached_status_without_inventing_zero(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    def offline(**kwargs):
        raise PanelError('offline')
    panel.usage = offline
    result = service.customer_subscription_status(actor, subscription_id=sid, refresh=True)
    assert result['sync_failed'] and result['usage_bytes'] == 1024**3
    assert handlers._subscription_menu_text().startswith('📊 وضعیت اشتراک')


def test_detail_lookup_is_not_limited_to_latest_500_subscriptions(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    item = service.customer_subscription_status(actor, subscription_id=sid, refresh=False)
    for _ in range(501):
        order = service.create_order(actor, item['plan_id'])
        conn.execute("INSERT INTO tenant_subscriptions (tenant_id,customer_id,plan_id,order_id,traffic_bytes,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (service.tenant_id, item['customer_id'], item['plan_id'], order['id'], 1024, item['expires_at'], iso_utc(utcnow()), iso_utc(utcnow())))
    conn.commit()
    assert sid not in [row['id'] for row in service.list_subscriptions(actor, limit=500)]
    assert service.customer_subscription_status(actor, subscription_id=sid, refresh=False)['id'] == sid


def test_completed_renewal_price_has_priority_over_catalog_and_pending_orders(conn, factories, cipher):
    service, panel, actor, sid, _ = _service(conn, factories, cipher)
    item = service.customer_subscription_status(actor, subscription_id=sid, refresh=False)
    for status, amount in (('fulfilled', 85000), ('pending_payment', 99000)):
        order = service.create_order(actor, item['plan_id'])
        conn.execute('UPDATE tenant_orders SET status=?,amount=? WHERE id=?', (status, amount, order['id']))
        conn.execute('INSERT INTO tenant_renewal_orders (tenant_id,order_id,subscription_id,created_at) VALUES (?,?,?,?)',
                     (service.tenant_id, order['id'], sid, iso_utc(utcnow())))
    conn.commit()
    result = service.customer_subscription_status(actor, subscription_id=sid, refresh=False)
    assert result['subscription_price'] == 85000


def test_refresh_ignores_telegram_message_not_modified():
    from telegram.error import BadRequest
    class Unchanged:
        async def edit_message_text(self, text, **kwargs):
            raise BadRequest('Message is not modified')
    asyncio.run(handlers._edit_subscription(Unchanged(), 'same'))
