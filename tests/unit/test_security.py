import base64
import hashlib
import hmac
import time
from uuid import uuid4

import jwt
import pytest

from app.integrations.livekit import LiveKitService
from app.meeting.security import sign_bridge, validate_meeting_url, verify_bridge, verify_webhook


@pytest.mark.parametrize('url', ['http://meet.google.com/abc-defg-hij',
    'https://meet.google.com.evil/abc-defg-hij', 'https://evil@meet.google.com/abc-defg-hij',
    'https://meet.google.com/abc-defg-hij?redirect=https://evil', 'https://127.0.0.1/abc-defg-hij'])
def test_meeting_url_rejects_untrusted_hosts(url):
    with pytest.raises(ValueError):
        validate_meeting_url(url)


def test_bridge_capability_rejects_expiry_and_tampering(settings):
    sid = str(uuid4())
    token = sign_bridge(sid, settings)
    assert verify_bridge(token, settings) == sid
    with pytest.raises(jwt.PyJWTError):
        verify_bridge(sign_bridge(sid, settings, int(time.time()) - 10), settings)
    with pytest.raises(jwt.PyJWTError):
        verify_bridge(token[:-8] + 'tampered', settings)


def signed_headers(settings, body, stamp=None, mid='event-1'):
    stamp = str(stamp or int(time.time()))
    key = base64.b64decode(settings.recall_webhook_secret.get_secret_value()[6:])
    sig = base64.b64encode(hmac.digest(key, mid.encode() + b'.' + stamp.encode() + b'.' + body,
                                      hashlib.sha256)).decode()
    return {'webhook-id': mid, 'webhook-timestamp': stamp, 'webhook-signature': 'v1,' + sig}


def test_signed_webhook_rejects_altered_body_and_replay(settings):
    body = b'{"event":"transcript.data"}'
    headers = signed_headers(settings, body)
    assert verify_webhook(body, headers, settings) == 'event-1'
    with pytest.raises(ValueError):
        verify_webhook(body + b' ', headers, settings)
    with pytest.raises(ValueError):
        verify_webhook(body, signed_headers(settings, body, int(time.time()) - 600), settings)


async def test_livekit_token_least_privilege(settings):
    service = LiveKitService(settings)
    sid = str(uuid4())
    try:
        data = jwt.decode(service.token(sid), settings.livekit_api_secret.get_secret_value(),
                          algorithms=['HS256'], options={'verify_aud': False})
        assert data['sub'] == 'input-' + sid
        assert data['video']['room'] == 'meeting-' + sid
        assert data['video']['canPublishSources'] == ['microphone']
        assert data['video']['canPublishData'] is True  # Scoped input-health diagnostics.
        assert data['exp'] - data['nbf'] <= 600
    finally:
        await service.close()
