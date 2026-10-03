"""Validated panel setup shared by the server wizard and edit menus.

Field meanings follow SellBot's AdminBot/servers.py; secrets remain encrypted
in the tenant credential table rather than copied into Telegram session state.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from TenantRuntime.panels import PanelError

SKIP = {'0', 'skip', '-', '_', '.', 'done', 'نه', 'خیر', '—', ''}


def optional(value: object) -> str:
    text = str(value or '').strip()
    return '' if text.lower() in SKIP else text


def url(value: object, *, domain: bool = False, required: bool = True) -> str:
    text = str(value or '').strip().rstrip('/')
    if domain:
        text = optional(text)
        if text and '://' not in text:
            text = 'https://' + text
    if not text and not required:
        return ''
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('invalid panel URL') from None
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or (port is not None and port == 0)
            or any(c.isspace() for c in text)
            or any(p in {'.', '..'} for p in parsed.path.split('/'))):
        raise ValueError('invalid panel URL')
    return text


def path(value: object, *, required: bool = True) -> str:
    text = str(value or '').strip().strip('/')
    if text.lower() in SKIP:
        if required:
            raise ValueError('panel path is required')
        text = ''
    if not text and not required:
        return ''
    if (not text or any(p in {'', '.', '..'} for p in text.split('/'))
            or any(c.isspace() for c in text) or any(c in text for c in '?#:\\')):
        raise ValueError('invalid panel path')
    return text


def inbound_ids(value: object, *, kind: str) -> str:
    text = str(value or '').strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    if text == '0':
        return '0'
    text = optional(text)
    if not text:
        return ''
    parts = re.split(r'[,،\s]+', text)
    if any(not p for p in parts):
        raise ValueError('invalid inbound ids')
    result = []
    for part in parts:
        if kind == 'xui':
            if not part.isdecimal() or int(part) <= 0:
                raise ValueError('invalid X-UI inbound ids')
            part = str(int(part))
        elif not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part == '0':
            raise ValueError('invalid X-NET inbound ids')
        if part not in result:
            result.append(part)
    return ','.join(result)


def normalize_settings(settings: dict) -> dict:
    result = dict(settings)
    kind = str(result.get('panel_kind') or result.get('provider_kind') or '').lower()
    if kind not in {'hiddify', 'xui', 'xnet'}:
        raise ValueError('unsupported panel provider')
    result['panel_kind'] = kind
    result['endpoint'] = url(result.get('endpoint'))
    for field in ('users_limit', 'priority'):
        number = int(result.get(field) or 0)
        if number < 0:
            raise ValueError('server number must be non-negative')
        result[field] = number
    if kind == 'hiddify':
        result['admin_path'] = path(result.get('admin_path'))
        result['user_path'] = path(result.get('user_path'))
    else:
        result[kind + '_inbound_ids'] = inbound_ids(result.get(kind + '_inbound_ids'), kind=kind)
        result[kind + '_public_origin'] = url(result.get(kind + '_public_origin'), domain=True, required=False)
        result[kind + '_sub_path'] = path(optional(result.get(kind + '_sub_path')) or 'sub')
        if kind == 'xui' and result.get('xui_flavor') not in {'sanaei', 'alireza'}:
            raise ValueError('invalid X-UI flavor')
        if kind == 'xnet':
            result['xnet_api_url'] = url(result.get('xnet_api_url'), required=False)
            port = int(result.get('xnet_sub_port') or 0)
            if not 0 <= port <= 65535:
                raise ValueError('invalid subscription port')
            result['xnet_sub_port'] = port
    return result


def credential(kind: str, *, flavor: str = '', values: dict) -> str:
    if kind == 'hiddify':
        raw = str(values.get('secret') or '').strip()
        if not raw or '/' in raw or any(c.isspace() for c in raw):
            raise ValueError('invalid Hiddify API key')
        return raw
    token = str(values.get('api_token') or '').strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    token = optional(token)
    user = str(values.get('username') or '').strip()
    password = str(values.get('password') or '')
    if kind == 'xui':
        if flavor not in {'sanaei', 'alireza'}:
            raise ValueError('invalid X-UI flavor')
        if flavor == 'alireza' and (not user or not password):
            raise ValueError('X-UI username and password are required')
        if flavor == 'sanaei' and not token and not (user and password):
            raise ValueError('Sanaei token or login is required')
        material = dict(version=1, flavor=flavor, api_token=token,
                        username=user, password=password,
                        secret_header=str(values.get('secret_header') or '').strip())
    elif kind == 'xnet':
        if not token and not password:
            raise ValueError('X-NET token or login is required')
        material = dict(version=1, provider='xnet', api_token=token,
                        username=user or 'admin', password=password)
    else:
        raise ValueError('unsupported panel provider')
    return json.dumps(material, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def connection_error(exc: Exception) -> str:
    """Translate safe adapter failures without echoing URLs or credentials."""
    text = str(exc).lower() if isinstance(exc, PanelError) else ''
    if 'fallback' in text:
        return '❌ توکن X-NET پذیرفته نشد. نام کاربری و رمز fallback را تنظیم کنید.'
    if '401' in text or '403' in text or 'authentication' in text or 'rejected' in text:
        return '❌ دسترسی پنل تأیید نشد. توکن یا نام کاربری و رمز را بررسی کنید.'
    if 'inbound' in text:
        return '❌ اینباند انتخاب‌شده فعال یا قابل استفاده نیست. اینباندهای پنل را بررسی کنید.'
    if '404' in text or 'json' in text or 'response' in text or 'list' in text:
        return '❌ مسیر API یا پاسخ پنل معتبر نیست. آدرس پنل و مسیر ادمین را بررسی کنید.'
    return '❌ اتصال پنل تأیید نشد. آدرس، پورت، مسیر و اطلاعات دسترسی را بررسی کنید.'
