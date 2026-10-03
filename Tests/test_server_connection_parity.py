"""Read-only panel probes, server wizards, retries and tenant-safe credential edits."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from TenantRuntime.AdminBot import handlers
from TenantRuntime.AdminBot.server_management import check_connection, access_values
from TenantRuntime.business import TenantBusinessService, TenantBusinessError
from TenantRuntime.hiddify import HiddifyPanelAdapter
from TenantRuntime.panels import PanelError, PanelTarget, RoutedPanelAdapter
from TenantRuntime.server_connections import normalize_settings, credential, connection_error
from TenantRuntime.xui import XuiPanelAdapter
from TenantRuntime.xnet import XnetPanelAdapter


class ProbePanel:
    def __init__(self):
        self.fail = False
        self.calls = []

    def inspect_connection(self, *, target, secret):
        self.calls.append((target, secret))
        if self.fail:
            raise PanelError('panel returned HTTP 401')
        return {'connected': True, 'users_count': 7, 'inbounds': []}


def service(conn, factories, cipher, panel=None):
    tenant = factories.tenant(owner_telegram_id=7001)
    return TenantBusinessService(conn, tenant_id=int(tenant['id']),
        owner_telegram_id=7001, secret_cipher=cipher, panel_adapter=panel or ProbePanel())


class Messages:
    def __init__(self):
        self.text = ''
        self.sent = []
        self.deleted = 0

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def send_message(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def delete(self):
        self.deleted += 1


class Query:
    def __init__(self, data, message):
        self.data, self.message = data, message
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.message.sent.append((text, kwargs))


def session(monkeypatch, business):
    message = Messages()
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7001),
        effective_message=message, effective_chat=message, callback_query=None)
    monkeypatch.setattr(handlers, '_services',
        lambda _: (SimpleNamespace(role='admin'), None, None, business))
    return update, context, message


async def click(update, context, data):
    update.callback_query = Query(data, update.effective_message)
    await handlers.on_callback(update, context)
    update.callback_query = None


async def send(update, context, text):
    update.effective_message.text = text
    await handlers.unknown_text(update, context)


@pytest.mark.parametrize('selected,inputs,kind,flavor', [
    ('hiddify', ['Turkey', 'https://panel.example', '/admin/', '/user/', '100', '2', 'hiddify-key'], 'hiddify', ''),
    ('xui_sanaei', ['France', 'https://panel.example:2053/base', '۱،۲', 'sub.example', '-', '100', '2', 'Bearer sanaei-key'], 'xui', 'sanaei'),
    ('xui_sanaei', ['France', 'https://panel.example/base', '0', '-', '-', '0', '0', 'admin | password-secret'], 'xui', 'sanaei'),
    ('xui_alireza', ['France', 'https://panel.example/base', 'skip', '-', '-', '100', '2', 'admin', 'password-secret'], 'xui', 'alireza'),
    ('xnet', ['France', 'http://panel.example:8080', 'in-a,in-b', 'sub.example', '443', 'subscription', '100', '2', 'Bearer xnet-key | admin | password-secret'], 'xnet', ''),
])
def test_all_add_wizards_probe_before_save(monkeypatch, conn, factories, cipher, selected, inputs, kind, flavor):
    business = service(conn, factories, cipher)
    update, context, message = session(monkeypatch, business)
    async def run():
        await click(update, context, 'srv:addtype:' + selected)
        for text in inputs:
            await send(update, context, text)
            assert 'password-secret' not in json.dumps(context.user_data)
            assert 'hiddify-key' not in json.dumps(context.user_data)
            assert 'sanaei-key' not in json.dumps(context.user_data)
            assert 'xnet-key' not in json.dumps(context.user_data)
    asyncio.run(run())
    rows = business.list_servers()
    assert len(rows) == 1
    assert rows[0]['panel_kind'] == kind
    assert (rows[0].get('xui_flavor') or '') == flavor
    assert 'biz_flow' not in context.user_data
    assert len(business.panel_adapter.calls) == 1
    assert message.deleted == 1
    assert any('✅ سرور با موفقیت اضافه شد' in text for text, _ in message.sent)
    encrypted = conn.execute('SELECT encrypted_secret FROM tenant_panel_credentials').fetchone()[0]
    assert inputs[-1] not in encrypted


def test_failed_add_retries_without_partial_server_or_secret_in_session(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    business.panel_adapter.fail = True
    update, context, message = session(monkeypatch, business)
    async def run():
        await click(update, context, 'srv:addtype:hiddify')
        for text in ['Turkey', 'https://panel.example', 'admin', 'user', '0', '0', 'bad-key']:
            await send(update, context, text)
        assert business.list_servers() == []
        assert context.user_data['biz_flow']['kind'] == 'server_add_secret'
        assert 'bad-key' not in json.dumps(context.user_data)
        assert any('دسترسی پنل تأیید نشد' in text for text, _ in message.sent)
        business.panel_adapter.fail = False
        await send(update, context, 'good-key')
        assert len(business.list_servers()) == 1
        await send(update, context, 'good-key')
        assert len(business.list_servers()) == 1
    asyncio.run(run())


def test_reconfigure_repairs_old_incomplete_record_and_preserves_id(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    server = business.add_server(7001, label='Old', panel_kind='hiddify', endpoint='https://old.example')
    update, context, message = session(monkeypatch, business)
    async def run():
        await click(update, context, f'srv:test:{server["id"]}')
        assert any('تنظیم مجدد اتصال' in text for text, _ in message.sent)
        await click(update, context, f'srv:reconnect:{server["id"]}')
        for text in ['New', 'https://new.example', 'admin', 'user', '50', '3', 'new-key']:
            await send(update, context, text)
    asyncio.run(run())
    assert len(business.list_servers()) == 1
    updated = business.server(server['id'])
    assert updated['endpoint'] == 'https://new.example'
    assert business.panel_status(server['id'])['configured']


def test_failed_edit_preserves_previous_connection_and_cancel_keyboard(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    server = business.add_server(7001, label='Old', panel_kind='hiddify',
        endpoint='https://old.example', admin_path='admin', user_path='user')
    business.set_panel_credential(7001, server_id=server['id'], secret='old-key')
    before = dict(conn.execute('SELECT * FROM tenant_panel_credentials').fetchone())
    update, context, message = session(monkeypatch, business)
    async def run():
        await click(update, context, f'srv:editf:{server["id"]}:credential')
        business.panel_adapter.fail = True
        await send(update, context, 'bad-key')
        assert business.server(server['id']) == server
        assert dict(conn.execute('SELECT * FROM tenant_panel_credentials').fetchone()) == before
        assert context.user_data['biz_flow']['kind'] == 'server_edit_field'
        assert message.sent[-1][1]['reply_markup'].keyboard[0][0].text == '❌ لغو'
        await send(update, context, '❌ لغو')
        assert 'biz_flow' not in context.user_data
        assert message.sent[-1][1]['reply_markup'].inline_keyboard
    asyncio.run(run())


def test_invalid_early_input_does_not_advance_wizard(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    update, context, message = session(monkeypatch, business)
    async def run():
        await click(update, context, 'srv:addtype:xui_sanaei')
        await send(update, context, 'France')
        await send(update, context, 'https://')
        assert context.user_data['biz_flow']['kind'] == 'server_add_endpoint'
        await send(update, context, 'https://panel.example')
        await send(update, context, '0,1')
        assert context.user_data['biz_flow']['kind'] == 'server_add_inbounds'
        assert business.list_servers() == []
        await send(update, context, '❌ لغو')
    asyncio.run(run())


@pytest.mark.parametrize('provider,flavor', [('hiddify',''),('xui','sanaei'),('xui','alireza'),('xnet','')])
def test_every_new_edit_button_enters_a_real_flow(monkeypatch, conn, factories, cipher, provider, flavor):
    business = service(conn, factories, cipher)
    server = business.add_server(7001, label='Panel', panel_kind=provider, xui_flavor=flavor,
        endpoint='https://panel.example', admin_path='admin', user_path='user')
    update, context, _ = session(monkeypatch, business)
    fields = [b.callback_data for row in handlers._server_edit_view(server).inline_keyboard
              for b in row if b.callback_data.startswith('srv:editf:')]
    async def run():
        for data in fields:
            await click(update, context, data)
            assert context.user_data['biz_flow']['kind'] == 'server_edit_field'
            assert context.user_data['biz_flow']['field'] == data.split(':')[-1]
    asyncio.run(run())


def test_partial_access_edits_preserve_other_encrypted_fields(conn, factories, cipher):
    business = service(conn, factories, cipher)
    server = business.add_server(7001, label='XNET', panel_kind='xnet', endpoint='https://panel.example')
    business.set_xnet_credential(7001, server_id=server['id'], api_token='token-original',
        username='admin', password='password-original')
    candidate, _, secret = business.prepare_server_connection(7001, server_id=server['id'],
        credentials={'password': 'password-new'})
    values = json.loads(secret)
    assert values['api_token'] == 'token-original'
    assert values['username'] == 'admin'
    assert values['password'] == 'password-new'
    business.commit_server_connection(7001, server_id=server['id'], candidate=candidate, secret=secret)
    _, _, saved = business.prepare_server_connection(7001, server_id=server['id'])
    assert json.loads(saved) == values


def test_atomic_commit_rolls_back_server_if_encryption_write_fails(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    candidate, _, secret = business.prepare_server_connection(7001, settings={
        'label':'Panel','panel_kind':'hiddify','endpoint':'https://panel.example',
        'admin_path':'admin','user_path':'user'}, credentials={'secret':'key'})
    def fail(**kwargs):
        raise TenantBusinessError('write failed')
    monkeypatch.setattr(business, '_store_panel_secret', fail)
    with pytest.raises(TenantBusinessError):
        business.commit_server_connection(7001, candidate=candidate, secret=secret)
    assert business.list_servers() == []


def test_connection_candidates_do_not_cross_tenants(conn, factories, cipher):
    a = service(conn, factories, cipher)
    b = service(conn, factories, cipher)
    server = a.add_server(7001, label='A', panel_kind='hiddify', endpoint='https://a.example', admin_path='admin', user_path='user')
    a.set_panel_credential(7001, server_id=server['id'], secret='a-key')
    with pytest.raises(TenantBusinessError):
        b.prepare_server_connection(7001, server_id=server['id'])
    with pytest.raises(PermissionError):
        a.prepare_server_connection(9999, server_id=server['id'])


@pytest.mark.parametrize('endpoint', ['https://','https://user:pass@panel.example', 'https://panel.example:99999',
    'https://panel.example?key=secret', 'https://panel.example/../bad'])
def test_invalid_panel_addresses_rejected(endpoint):
    with pytest.raises(ValueError):
        normalize_settings(dict(panel_kind='hiddify',endpoint=endpoint,admin_path='admin',user_path='user'))


def test_hiddify_probe_uses_protected_users_api_and_never_mutates_users():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.headers['Hiddify-API-Key'] == 'key'
        if request.url.path.endswith('/panel/info/'):
            return httpx.Response(200,json={'version':'12.3.3'})
        return httpx.Response(200,json=[{'uuid':'one'}, {'uuid':'two'}])
    target = PanelTarget(kind='hiddify',endpoint='https://panel.example',admin_path='admin',user_path='user')
    result = HiddifyPanelAdapter(transport=httpx.MockTransport(handler)).inspect_connection(target=target, secret='key')
    assert result['users_count'] == 2 and result['panel_major'] == 12
    assert requests[0].url.path == '/admin/api/v2/admin/user/'


@pytest.mark.parametrize('payload', [{'status':'ok'}, '<html>login</html>', [1]])
def test_hiddify_probe_does_not_accept_homepage_or_malformed_users(payload):
    def handler(request):
        return httpx.Response(200, json=payload)
    target = PanelTarget(kind='hiddify',endpoint='https://panel.example',admin_path='admin',user_path='user')
    with pytest.raises(PanelError):
        HiddifyPanelAdapter(transport=httpx.MockTransport(handler)).inspect_connection(target=target, secret='key')


@pytest.mark.parametrize('flavor,values', [('sanaei',{'api_token':'token'}),
    ('sanaei',{'username':'admin','password':'pass'}),
    ('alireza',{'username':'admin','password':'pass'})])
def test_xui_probe_preserves_panel_path_and_verifies_selected_inbounds(flavor, values):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path.startswith('/base/')
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'success':True},headers={'set-cookie':'session=ok; Path=/'})
        if values.get('api_token'):
            assert request.headers['authorization'] == 'Bearer token'
        else:
            assert 'session=ok' in request.headers['cookie']
        return httpx.Response(200,json={'success':True,'obj':[{'id':1,'enable':True,'protocol':'vless', 'settings':'{"clients":[]}'},
            {'id':2,'enable':False,'protocol':'vless'}]})
    adapter = XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target = PanelTarget(kind='xui',endpoint='https://panel.example/base',xui_flavor=flavor,xui_inbound_ids='1')
    result = adapter.inspect_connection(target=target,secret=credential('xui',flavor=flavor,values=values))
    assert result['selected_inbound_ids'] == ['1']
    assert all(r.method == 'GET' or r.url.path.endswith('/login') for r in seen)
    from dataclasses import replace
    with pytest.raises(PanelError):
        adapter.inspect_connection(target=replace(target,xui_inbound_ids='2'), secret=credential('xui',flavor=flavor,values=values))


def test_xnet_internal_api_and_jwt_fallback_do_not_change_public_subscription():
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.url.host == 'internal.example'
        if request.url.path.endswith('/ping'):
            return httpx.Response(200,json={'success':True})
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'token':'jwt-good'})
        if request.headers.get('authorization') == 'Bearer rejected':
            return httpx.Response(401,json={'error':'unauthorized'})
        assert request.headers['authorization'] == 'Bearer jwt-good'
        return httpx.Response(200,json=[{'id':'in-a','enabled':True,'protocol':'vless','clients':[]}])
    target=PanelTarget(kind='xnet',endpoint='https://public.example',xnet_api_url='http://internal.example:8080',
        xnet_public_origin='https://sub.example',xnet_sub_port=443)
    adapter=XnetPanelAdapter(transport=httpx.MockTransport(handler))
    secret=credential('xnet',values={'api_token':'Bearer rejected','username':'admin','password':'pass'})
    result=adapter.inspect_connection(target=target,secret=secret)
    assert result['selected_inbound_ids'] == ['in-a']
    assert adapter.subscription_link(target=target,external_ref='uuid') == 'https://sub.example/sub/uuid'
    assert len([r for r in seen if r.url.path.endswith('/login')]) == 1


def test_disabled_xnet_inbound_is_not_accepted_for_sales():
    def handler(request):
        return httpx.Response(200,json={} if request.url.path.endswith('/ping') else [{'id':'in-off','enabled':False}])
    with pytest.raises(PanelError):
        XnetPanelAdapter(transport=httpx.MockTransport(handler)).inspect_connection(
            target=PanelTarget(kind='xnet',endpoint='https://panel.example'),secret='token')


def test_safe_connection_errors_never_echo_endpoint_or_secret():
    assert 'secret' not in connection_error(RuntimeError('https://panel.example secret'))
    assert 'panel.example' not in connection_error(PanelError('panel returned HTTP 403 https://panel.example secret'))


def test_router_does_not_claim_success_for_adapter_without_inspection():
    with pytest.raises(PanelError):
        RoutedPanelAdapter({'hiddify':object()}).inspect_connection(
            target=PanelTarget(kind='hiddify',endpoint='https://panel.example'),secret='key')


def test_inline_back_clears_pending_edit_and_restores_main_keyboard(monkeypatch, conn, factories, cipher):
    business = service(conn, factories, cipher)
    server = business.add_server(7001,label='Panel',panel_kind='hiddify',endpoint='https://panel.example',admin_path='admin',user_path='user')
    update,context,message=session(monkeypatch,business)
    async def run():
        await click(update,context,f'srv:editf:{server["id"]}:credential')
        await click(update,context,'biz:servers')
        assert 'biz_flow' not in context.user_data
        assert message.sent[-2][1]['reply_markup'].keyboard == handlers.admin_main_keyboard().keyboard
    asyncio.run(run())


def test_sanaei_cookie_login_create_disable_usage_renew_and_delete():
    from Tests.test_xui_live import _request
    from TenantRuntime.panels import RenewRequest
    clients=[]
    writes=[]
    resets=[]
    def handler(request):
        path=request.url.path
        body=json.loads(request.content) if request.content else {}
        if path.endswith('/login'):
            return httpx.Response(200,json={'success':True},headers={'set-cookie':'session=ok; Path=/'})
        assert request.headers.get('authorization') is None
        assert 'session=ok' in request.headers.get('cookie','')
        assert '/panel/api/' in path
        if path.endswith('/inbounds/list'):
            return httpx.Response(200,json={'success':True,'obj':[dict(id=1,protocol='vless',enable=True,
                settings=json.dumps({'clients':clients}), clientStats=[dict(email=c['email'],up=10,down=20) for c in clients])]})
        if path.endswith('/inbounds/addClient'):
            clients.extend(json.loads(body['settings'])['clients'])
        elif '/inbounds/updateClient/' in path:
            writes.append(body)
            updated=json.loads(body['settings'])['clients'][0]
            clients[:] = [updated if c['id']==updated['id'] else c for c in clients]
        elif '/resetClientTraffic/' in path:
            resets.append(path)
        elif '/delClient/' in path:
            clients[:] = [c for c in clients if c['id'] != path.split('/')[-1]]
        else:
            return httpx.Response(200,json={'success':True,'obj':[]})
        return httpx.Response(200,json={'success':True})
    adapter=XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target=PanelTarget(kind='xui',endpoint='https://panel.example',xui_flavor='sanaei')
    secret=credential('xui',flavor='sanaei',values={'username':'admin','password':'pass'})
    created=adapter.provision(target=target,secret=secret,request=_request())
    adapter.set_enabled(target=target,secret=secret,external_ref=created.external_ref,enabled=False)
    assert len(clients)==1 and not clients[0]['enable']
    assert clients[0]['totalGB']==20*1024**3
    assert resets==[]
    assert adapter.usage(target=target,secret=secret,external_ref=created.external_ref).usage_bytes==30
    renewed=adapter.renew(target=target,secret=secret,external_ref=created.external_ref,
        request=RenewRequest(traffic_bytes=30*1024**3,duration_days=30,expires_at=_request().expires_at))
    assert renewed.active and renewed.traffic_bytes==30*1024**3
    assert resets[0].endswith('/resetClientTraffic/'+clients[0]['email'])
    adapter.delete_user(target=target,secret=secret,external_ref=created.external_ref)
    assert clients==[]


def test_sanaei_rejected_token_falls_back_to_cookie_per_inbound_api():
    seen=[]
    def handler(request):
        seen.append(request)
        if request.headers.get('authorization')=='Bearer bad-token':
            return httpx.Response(401,json={'error':'bad token'})
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'success':True},headers={'set-cookie':'session=ok; Path=/'})
        assert 'session=ok' in request.headers['cookie']
        return httpx.Response(200,json={'success':True,'obj':[{'id':1,'enable':True,'protocol':'vless'}]})
    adapter=XuiPanelAdapter(transport=httpx.MockTransport(handler))
    target=PanelTarget(kind='xui',endpoint='https://panel.example',xui_flavor='sanaei')
    secret=credential('xui',flavor='sanaei',values={'api_token':'bad-token','username':'admin','password':'pass'})
    result=adapter.inspect_connection(target=target,secret=secret)
    assert result['connected']
    assert len([r for r in seen if r.url.path.endswith('/login')])==1


def test_changing_xnet_password_cannot_reuse_old_cached_jwt():
    logins=[]
    def handler(request):
        if request.url.path.endswith('/ping'):
            return httpx.Response(200,json={'ok':True})
        if request.url.path.endswith('/login'):
            password=json.loads(request.content)['password']
            logins.append(password)
            if password=='wrong':
                return httpx.Response(401,json={'error':'bad password'})
            return httpx.Response(200,json={'token':'jwt-good'})
        return httpx.Response(200,json=[{'id':'in-a','enabled':True,'clients':[]}])
    adapter=XnetPanelAdapter(transport=httpx.MockTransport(handler))
    target=PanelTarget(kind='xnet',endpoint='https://panel.example')
    adapter.inspect_connection(target=target,secret=credential('xnet',values={'password':'correct'}))
    with pytest.raises(PanelError):
        adapter.inspect_connection(target=target,secret=credential('xnet',values={'password':'wrong'}))
    assert logins==['correct','wrong']


@pytest.mark.parametrize('admin_path', ['-', 'skip', '0', 'https://panel.example/admin', 'admin?key=secret'])
def test_hiddify_admin_path_requires_actual_proxy_path(admin_path):
    with pytest.raises(ValueError):
        normalize_settings(dict(panel_kind='hiddify', endpoint='https://panel.example',
            admin_path=admin_path, user_path='user'))


def test_persian_zero_selects_all_inbounds_and_optional_sub_path_defaults():
    settings = normalize_settings(dict(panel_kind='xui',xui_flavor='sanaei',
        endpoint='https://panel.example',xui_inbound_ids='۰',xui_sub_path='-'))
    assert settings['xui_inbound_ids']=='0'
    assert settings['xui_sub_path']=='sub'
