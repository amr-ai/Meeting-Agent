import base64
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import jwt

from app.config.settings import Settings


def validate_meeting_url(value: str) -> str:
    meeting_platform(value)
    return value


def meeting_platform(value: str) -> str:
    """Allow meeting links, never arbitrary URLs or redirect endpoints."""
    error = 'Use an HTTPS Google Meet, Zoom /j/, or Microsoft Teams meeting link'
    if len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError(error)
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.username or url.password or url.port
                or url.fragment):
            raise ValueError(error)
        host = url.hostname or ''
        if host == 'meet.google.com' and re.fullmatch(r'/[a-z]{3}-[a-z]{4}-[a-z]{3}', url.path) and not url.query:
            return 'google_meet'
        if (host == 'zoom.us' or host.endswith('.zoom.us')) and re.fullmatch(r'/j/\d{9,11}/?', url.path):
            if set(parse_qs(url.query, keep_blank_values=True)) <= {'pwd', 'omn'}:
                return 'zoom'
        if host in {'teams.microsoft.com', 'teams.live.com', 'teams.cloud.microsoft'}:
            if (re.fullmatch(r'/l/meetup-join/[^/]+/\d+/?', url.path)
                    or re.fullmatch(r'/meet/\d+/?', url.path)):
                if set(parse_qs(url.query, keep_blank_values=True)) <= {'context', 'p'}:
                    return 'microsoft_teams'
    except ValueError:
        pass
    raise ValueError(error)


def room_name(sid: str) -> str:
    return f'meeting-{UUID(sid)}'


def sign_bridge(sid: str, settings: Settings, expires: int | None = None) -> str:
    now = int(time.time())
    return jwt.encode({'sub': str(UUID(sid)), 'iat': now,
                       'exp': expires or now + settings.bridge_ttl_seconds,
                       'iss': 'meeting-agent', 'aud': 'bridge'},
                      settings.bridge_signing_secret.get_secret_value(), algorithm='HS256')


def verify_bridge(token: str, settings: Settings) -> str:
    data = jwt.decode(token, settings.bridge_signing_secret.get_secret_value(),
                      algorithms=['HS256'], issuer='meeting-agent', audience='bridge',
                      options={'require': ['sub', 'exp', 'iat', 'iss', 'aud']})
    return str(UUID(data['sub']))


def verify_webhook(body: bytes, headers: dict[str, str], settings: Settings) -> str:
    legacy = 'svix-id' in headers and 'webhook-id' not in headers
    prefix = 'svix' if legacy else 'webhook'
    secret = (settings.recall_legacy_webhook_secret if legacy else
              settings.recall_webhook_secret).get_secret_value()
    mid = headers.get(f'{prefix}-id', '')
    stamp = headers.get(f'{prefix}-timestamp', '')
    signatures = headers.get(f'{prefix}-signature', '')
    if not secret.startswith('whsec_') or not mid or abs(time.time() - int(stamp)) > 300:
        raise ValueError('Invalid webhook authentication')
    key = base64.b64decode(secret[6:], validate=True)
    expected = base64.b64encode(hmac.digest(key, mid.encode() + b'.' + stamp.encode() + b'.' + body,
                                          hashlib.sha256)).decode()
    if not any(hmac.compare_digest(s, 'v1,' + expected) for s in signatures.split()):
        raise ValueError('Invalid webhook signature')
    return mid


def sign_video(sid: str, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode({'sub': str(UUID(sid)), 'iat': now, 'exp': now + settings.session_max_seconds,
                       'iss': 'meeting-agent', 'aud': 'recall-video'},
                      settings.bridge_signing_secret.get_secret_value(), algorithm='HS256')


def verify_video(token: str, sid: str, settings: Settings) -> None:
    data = jwt.decode(token, settings.bridge_signing_secret.get_secret_value(), algorithms=['HS256'],
                      issuer='meeting-agent', audience='recall-video',
                      options={'require': ['sub', 'iat', 'exp', 'iss', 'aud']})
    if data['sub'] != str(UUID(sid)):
        raise ValueError('Screen capability belongs to another session')
