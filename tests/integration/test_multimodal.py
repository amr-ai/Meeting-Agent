"""Mocked meeting integration, not a real Google Meet end-to-end claim."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app
from app.meeting.security import sign_video
from tests.integration.test_api import FakeLiveKit, FakeRecall
from tests.unit.test_screen_reception import event, h264_packets
from tests.unit.test_screen_vision import Provider


class MeetingRecall(FakeRecall):
    def __init__(self):
        self.sent = []

    async def send_chat_message(self, bot, message):
        self.sent.append((bot, message))
        return {'accepted': True}


class Text:
    async def generate(self, message, previous, sources):
        return 'Verified solution: https://example.com/fix'


class Search:
    async def run(self, query):
        return {'sources': [{'title': 'Verified solution', 'url': 'https://example.com/fix'}]}


def wait_screen(client, sid, auth, pid):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        data = client.get(f'/api/meetings/{sid}/screens', headers=auth).json()
        matches = [screen for screen in data['screens'] if screen['presenter_id'] == pid and screen['fresh']]
        if matches:
            return matches[0]
        time.sleep(.02)
    raise AssertionError('Mock screen was not decoded')


def send_screen(socket, sid, pid, name, start):
    for index, packet in enumerate(h264_packets()):
        payload = event(sid, packet, start + index / 10, pid=pid, name=name)
        payload['data']['bot']['id'] = 'provider-bot'
        socket.send_json(payload)


def test_mock_meeting_screen_question_presenter_switch_research_stop_independence_and_reconnect(settings):
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    recall, provider = MeetingRecall(), Provider()
    app = create_app(cfg, FakeLiveKit(), recall, Text(), vision_provider=provider)
    auth = {'Authorization': 'Bearer ' + cfg.admin_api_key.get_secret_value()}
    with TestClient(app) as client:
        app.state.chat.workflow.search = Search()
        sid = client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': True}).json()['session_id']
        token = sign_video(sid, cfg)
        with client.websocket_connect(f'/ws/recall-video/{sid}/?token={token}') as socket:
            first = event(sid, b'', 1, event_type='participant_events.screenshare_on')
            first['data']['bot']['id'] = 'provider-bot'
            socket.send_json(first)
            send_screen(socket, sid, '7', 'Ahmed', 2)
            frame = wait_screen(client, sid, auth, '7')
            body = {'question': 'Mohamed asks about Ahmed error on screen', 'presenter': 'Ahmed',
                    'expected': {key: frame[key] for key in ('stream_id', 'generation', 'revision')}}
            assert client.post(f'/api/meetings/{sid}/vision', json=body).status_code == 401
            assert client.post(f'/api/meetings/{sid}/screen-frame', json=body).status_code == 401
            captured = client.post(f'/api/meetings/{sid}/screen-frame', headers=auth, json=body).json()
            assert captured['image'] and captured['source']['participant_id'] == '7'
            assert not provider.calls  # Native capture is not a separate vision-model request.
            body['expected'] = {key: captured['source'][key] for key in ('stream_id', 'generation', 'revision')}
            answer = client.post(f'/api/meetings/{sid}/vision', headers=auth, json=body).json()
            assert answer['source']['participant_id'] == '7' and answer['readable']
            research = {**body, 'research_to_chat': True, 'idempotency_key': 'screen-research:meeting-command'}
            queued = client.post(f'/api/meetings/{sid}/vision', headers=auth, json=research).json()
            assert queued['job_id'] and not queued['accepted']
            repeated = client.post(f'/api/meetings/{sid}/vision', headers=auth, json=research).json()
            assert repeated['job_id'] == queued['job_id']
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not recall.sent:
                time.sleep(.03)
            assert recall.sent == [('provider-bot', 'Verified solution: https://example.com/fix')]
            off = event(sid, b'', 10, event_type='participant_events.screenshare_off')
            off['data']['bot']['id'] = 'provider-bot'
            socket.send_json(off)
            send_screen(socket, sid, '8', 'Sara', 11)
            wait_screen(client, sid, auth, '8')
            result = client.post(f'/api/meetings/{sid}/vision', headers=auth,
                                 json={'question': 'Explain Sara screen', 'presenter': 'Sara'}).json()
            assert result['source']['participant_id'] == '8'
            old = client.post(f'/api/meetings/{sid}/vision', headers=auth, json=body).json()
            assert 'error' in old
        # A temporary video disconnect does not leave the bot or break chat/voice session state.
        assert client.get(f'/api/meetings/{sid}', headers=auth).json()['status'] not in {'left', 'failed'}
        assert client.post(f'/api/meetings/{sid}/vision', headers=auth, json={'question': 'Explain now'}).json().get('error')
        with client.websocket_connect(f'/ws/recall-video/{sid}/?token={token}') as socket:
            send_screen(socket, sid, '8', 'Sara', 20)
            wait_screen(client, sid, auth, '8')
            assert client.post(f'/api/meetings/{sid}/vision', headers=auth,
                               json={'question': 'New screen', 'presenter': 'Sara'}).json()['readable']
        assert len(recall.sent) == 1


def test_api_remains_responsive_while_vision_request_is_waiting(settings):
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    provider = Provider()
    provider.release.clear()
    app = create_app(cfg, FakeLiveKit(), MeetingRecall(), Text(), vision_provider=provider)
    auth = {'Authorization': 'Bearer ' + cfg.admin_api_key.get_secret_value()}
    with TestClient(app) as client:
        sid = client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': True}).json()['session_id']
        with client.websocket_connect(f'/ws/recall-video/{sid}/?token={sign_video(sid, cfg)}') as socket:
            send_screen(socket, sid, '7', 'Ahmed', 1)
            wait_screen(client, sid, auth, '7')
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, f'/api/meetings/{sid}/vision', headers=auth,
                                      json={'question': 'Slow visual question'})
                async def started():
                    await asyncio.wait_for(provider.started.wait(), 2)
                client.portal.call(started)
                assert not pending.done()
                assert client.get('/health').status_code == 200
                assert client.get(f'/api/meetings/{sid}/chat', headers=auth).status_code == 200
                client.portal.call(provider.release.set)
                assert pending.result(timeout=3).json()['readable']
