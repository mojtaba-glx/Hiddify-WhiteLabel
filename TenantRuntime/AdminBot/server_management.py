"""Connection UI and safe, read-only probes for the tenant server menus."""
from __future__ import annotations

import asyncio
from TenantRuntime.panels import PanelError

ACCESS_FIELDS = {
    'hiddify': {'credential'},
    'xui': {'credential', 'api_token', 'username', 'password', 'secret_header'},
    'xnet': {'credential', 'api_token', 'username', 'password'},
}
SETTING_FIELDS = {
    'hiddify': {'admin_path', 'user_path'},
    'xui': {'xui_public_origin', 'xui_inbound_ids', 'xui_sub_path'},
    'xnet': {'xnet_public_origin', 'xnet_inbound_ids', 'xnet_sub_path',
             'xnet_sub_port', 'xnet_api_url'},
}
COMMON_FIELDS = {'label', 'endpoint', 'users_limit', 'priority'}


def access_values(provider: str, flavor: str, text: str) -> dict:
    if provider == 'hiddify':
        return {'secret': text}
    parts = [part.strip() for part in text.split('|')]
    if provider == 'xui':
        if flavor == 'sanaei' and len(parts) == 1:
            return {'api_token': parts[0], 'username': '', 'password': ''}
        if len(parts) in {2, 3}:
            return {'username': parts[0], 'password': parts[1],
                    'api_token': parts[2] if flavor == 'sanaei' and len(parts) == 3 else '',
                    'secret_header': parts[2] if flavor == 'alireza' and len(parts) == 3 else ''}
        if flavor == 'sanaei' and len(parts) == 4:
            return {'username': parts[0], 'password': parts[1],
                    'api_token': parts[2], 'secret_header': parts[3]}
        raise ValueError('invalid X-UI credential')
    if len(parts) == 1:
        return {'api_token': parts[0]}
    if len(parts) == 3:
        return {'api_token': parts[0], 'username': parts[1], 'password': parts[2]}
    raise ValueError('invalid X-NET credential')


async def check_connection(business, actor: int, **kwargs):
    candidate, target, secret = business.prepare_server_connection(actor, **kwargs)
    # SQLite is used on the event-loop thread; only the pure HTTP adapter runs
    # in a worker. A failed probe leaves the previous server and secret intact.
    result = await asyncio.to_thread(
        business.panel_adapter.inspect_connection, target=target, secret=secret,
    )
    if not isinstance(result, dict) or result.get('connected') is not True:
        raise PanelError('panel connection could not be verified')
    return candidate, secret, result


def inspection_text(result: dict) -> str:
    lines = ['✅ اتصال پنل و دسترسی API تأیید شد.',
             f"👤 تعداد کاربران پنل: {int(result.get('users_count') or 0)}"]
    if result.get('panel_major'):
        lines.append(f"📦 نسخه Hiddify: {int(result['panel_major'])}")
    inbounds = result.get('inbounds') or []
    if inbounds:
        selected = {str(x) for x in result.get('selected_inbound_ids') or []}
        lines.append('\n🧩 اینباندهای پنل:')
        for row in inbounds[:20]:
            marker = '✅' if row.get('enabled') else '⛔'
            chosen = ' • انتخاب‌شده' if str(row['id']) in selected else ''
            lines.append(f"{marker} {str(row['id'])[:50]} | {str(row.get('protocol') or '-')[:30]} | "
                         f"{str(row.get('remark') or '-')[:80]}{chosen}")
        if len(inbounds) > 20:
            lines.append(f'… مجموع {len(inbounds)} اینباند')
    elif 'selected_inbound_ids' in result:
        lines.append('⚠️ هنوز اینباندی ساخته نشده؛ پیش از فروش در پنل اینباند بسازید.')
    return '\n'.join(lines)
