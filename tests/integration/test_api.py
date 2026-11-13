import json

from fastapi.testclient import TestClient

from app.main import create_app
from tests.unit.test_security import signed_headers


class FakeLiveKit:
    def __init__(self):
        self.rooms = set()

    async def ensure(self, meeting):
        self.rooms.add(meeting['id'])

    def token(self, sid):
        return 'mock-room-token'

    async def cleanup(self, sid):
        self.rooms.discard(sid)

    async def close(self):
        pass


class FakeRecall:
    async def create(self, meeting_url, bridge_url):
        assert bridge_url.startswith('https://agent.example/bridge#')
        return {'id': 'provider-bot'}

    async def leave(self, bot_id):
        pass

    async def status(self, bot_id):
        return {'status_changes': [{'code': 'in_call_recording'}]}

    async def final_transcript(self, bot_id):
        return None


def test_authorized_local_session_renewal_and_revocation(settings):
    lk = FakeLiveKit()
    with TestClient(create_app(settings, lk, FakeRecall())) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        assert client.post('/api/local').status_code == 401
        created = client.post('/api/local', headers=auth).json()
        sid = created['session_id']
        bridge = client.post('/api/bridge/session', json={'token': created['bridge_token']})
        assert bridge.status_code == 200
        assert bridge.json()['session_id'] == sid
        assert client.get(f'/api/meetings/{sid}/transcript').status_code == 401
        assert client.post(f'/api/meetings/{sid}/leave', headers=auth).status_code == 200
        assert client.post('/api/bridge/session', json={'token': bridge.json()['bridge_token']}).status_code == 410
        assert sid not in lk.rooms


def test_meeting_join_signed_webhook_and_deletion(settings):
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall())) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        assert client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': False}).status_code == 400
        joined = client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': True})
        assert joined.status_code == 200
        sid = joined.json()['session_id']
        payload = {'event': 'transcript.data', 'data': {'bot': {'id': 'provider-bot'}, 'data': {
            'participant': {'id': 7, 'name': 'Alice'}, 'words': [{'text': 'Hello',
                'start_timestamp': {'relative': 1}, 'end_timestamp': {'relative': 2}}]}}}
        body = json.dumps(payload).encode()
        assert client.post('/webhooks/recall/', content=body).status_code == 401
        headers = signed_headers(settings, body)
        assert client.post('/webhooks/recall/', content=body, headers=headers).status_code == 200
        assert client.post('/webhooks/recall/', content=body, headers=headers).status_code == 200
        assert client.delete(f'/api/meetings/{sid}', headers=auth).status_code == 409
        assert client.post(f'/api/meetings/{sid}/leave', headers=auth).status_code == 200
        assert client.delete(f'/api/meetings/{sid}', headers=auth).status_code == 200
        assert client.get(f'/api/meetings/{sid}', headers=auth).status_code == 404


def test_authenticated_live_context_and_assistant_history(settings):
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall())) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        sid = client.post('/api/local', headers=auth).json()['session_id']
        path = f'/api/meetings/{sid}'
        assert client.get(path + '/live-context').status_code == 401
        item = {'event_key': 'assistant-1', 'role': 'assistant', 'text': 'Earlier reply', 'interrupted': True}
        assert client.post(path + '/conversation', json=item).status_code == 401
        assert client.post(path + '/conversation', headers=auth, json=item).status_code == 200
        assert client.post(path + '/conversation', headers=auth, json=item).status_code == 200
        result = client.get(path + '/live-context', headers=auth).json()
        assert len(result['messages']) == 1
        assert result['messages'][0]['speaker'] == settings.assistant_name
        assert result['messages'][0]['interrupted']
        second = client.get(path + '/live-context', headers=auth, params={'after': result['cursor']}).json()
        assert not second['messages']
        persona_item = {**item, 'event_key': 'mariam-1', 'speaker': 'Mariam', 'text': 'Client objection'}
        assert client.post(path + '/conversation', headers=auth, json=persona_item).status_code == 200
        messages = client.get(path + '/live-context', headers=auth).json()['messages']
        assert [m['speaker'] for m in messages] == [settings.assistant_name, 'Mariam']
        invalid = {**persona_item, 'speaker': 'Unconfigured identity'}
        assert client.post(path + '/conversation', headers=auth, json=invalid).status_code == 422


def test_bridge_clock_latency_is_scoped_and_validated(settings):
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall())) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        created = client.post('/api/local', headers=auth).json()
        sid = created['session_id']
        item = {'token': created['bridge_token'], 'turn_id': 'measured-turn', 'duration_ms': 1250,
                'speech_end_ms': 100, 'first_received_audio_ms': 1350, 'clock_origin_ms': 100000}
        assert client.post('/api/bridge/latency', json={**item, 'token': 'invalid'}).status_code == 401
        assert client.post('/api/bridge/latency', json={**item, 'duration_ms': 10}).status_code == 422
        assert client.post('/api/bridge/latency', json=item).status_code == 200
        assert client.post('/api/bridge/latency', json=item).status_code == 200
        assert client.get(f'/api/meetings/{sid}/latency').status_code == 401
        report = client.get(f'/api/meetings/{sid}/latency', headers=auth).json()
        assert report['samples'] == 1 and report['p50_ms'] == 1250


def test_local_ui_needs_no_admin_key_and_returns_only_session_capability(settings):
    lk = FakeLiveKit()
    with TestClient(create_app(settings, lk, FakeRecall()), base_url='http://localhost:8000',
                    client=('127.0.0.1', 50000)) as client:
        headers = {'Origin': 'http://localhost:8000', 'X-Voice-UI': '1'}
        response = client.post('/api/ui/session', headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert set(data) == {'session_id', 'bridge_token'}
        assert settings.admin_api_key.get_secret_value() not in response.text
        assert client.post('/api/local').status_code == 401
        assert client.post('/api/ui/leave', headers=headers,
                           json={'token': data['bridge_token']}).status_code == 200
        assert data['session_id'] not in lk.rooms
        assert client.post('/api/bridge/session', json={'token': data['bridge_token']}).status_code == 410


def test_keyless_ui_rejects_public_hosts_cross_origin_and_forwarded_requests(settings):
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall()), base_url='http://localhost:8000',
                    client=('127.0.0.1', 50000)) as client:
        valid = {'Origin': 'http://localhost:8000', 'X-Voice-UI': '1'}
        for headers in ({}, {**valid, 'Origin': 'https://evil.example'},
                        {**valid, 'Host': 'public.ngrok-free.dev'},
                        {**valid, 'X-Forwarded-For': '127.0.0.1'},
                        {**valid, 'Host': 'evil.example', 'Origin': 'http://evil.example'}):
            assert client.post('/api/ui/session', headers=headers).status_code == 403
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall()), base_url='http://localhost:8000',
                    client=('192.0.2.1', 50000)) as client:
        assert client.post('/api/ui/session', headers=valid).status_code == 403


def test_local_ui_cannot_leave_a_meeting_with_its_bridge_token(settings):
    from app.meeting.security import sign_bridge
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall()), base_url='http://localhost:8000',
                    client=('127.0.0.1', 50000)) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        sid = client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': True}).json()['session_id']
        assert client.post('/api/ui/leave', headers={'Origin': 'http://localhost:8000', 'X-Voice-UI': '1'},
                           json={'token': sign_bridge(sid, settings)}).status_code == 403
