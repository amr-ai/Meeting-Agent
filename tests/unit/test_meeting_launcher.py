import json

import httpx
import pytest
from pydantic import SecretStr

from scripts import launch, meeting_tunnel


def test_service_restart_keeps_healthy_peer(monkeypatch, capsys):
    class Child:
        def __init__(self, pid, code=None):
            self.pid, self.code = pid, code

        def poll(self):
            return self.code

    healthy = Child(1)
    replacement = Child(3)
    children = [healthy, Child(2, 7)]
    clock = [100.]
    spawns = []

    def spawn(command, **kwargs):
        spawns.append(command)
        return replacement

    def sleep(seconds):
        clock[0] += seconds
        if spawns:
            raise KeyboardInterrupt

    monkeypatch.setattr(launch.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(launch.time, 'sleep', sleep)
    monkeypatch.setattr(launch.subprocess, 'Popen', spawn)
    with pytest.raises(KeyboardInterrupt):
        launch.supervise(children, [['api'], ['worker']], 0)
    assert children == [healthy, replacement]
    assert spawns == [['worker']]
    assert 'voice worker exited (PID 2, code 7)' in capsys.readouterr().out


def test_monitor_retries_connection_loss_without_leaving_or_rejoining(monkeypatch, settings, tmp_path, capsys):
    calls = []
    polls = 0

    def handler(request):
        nonlocal polls
        calls.append(request)
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'protocol_version': 12})
        if request.url.path == '/api/meetings':
            return httpx.Response(200, json={'session_id': 'session-test'})
        if request.url.path.endswith('/leave'):
            assert polls == 4
            return httpx.Response(200, json={})
        if request.url.path.endswith('/live-context'):
            if polls == 2:
                raise httpx.ReadTimeout('offline feed failure', request=request)
            return httpx.Response(200, json={'messages': [], 'participants': [], 'active_speakers': [], 'cursor': 0})
        polls += 1
        if polls == 1:
            raise httpx.ConnectError('offline API restart', request=request)
        return httpx.Response(200, json={'status': 'done' if polls == 4 else 'in_call_recording'})

    configure(monkeypatch, settings, tmp_path, handler)
    assert meeting_tunnel.main() == 0
    assert sum(r.url.path == '/api/meetings' for r in calls) == 1
    assert sum(r.url.path.endswith('/leave') for r in calls) == 1
    assert 'Meeting monitoring reconnected.' in capsys.readouterr().out


def test_ctrl_c_during_monitor_failure_still_leaves(monkeypatch, settings, tmp_path):
    leaves = []

    def handler(request):
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'protocol_version': 12})
        if request.url.path == '/api/meetings':
            return httpx.Response(200, json={'session_id': 'session-test'})
        if request.url.path.endswith('/leave'):
            leaves.append(request)
            return httpx.Response(200, json={})
        raise httpx.ConnectError('offline', request=request)

    configure(monkeypatch, settings, tmp_path, handler)

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(meeting_tunnel.time, 'sleep', interrupt)
    assert meeting_tunnel.main() == 0
    assert len(leaves) == 1


def configure(monkeypatch, settings, tmp_path, handler):
    settings = settings.model_copy(update={'ngrok_authtoken': SecretStr('offline-test-token')})
    monkeypatch.setattr(meeting_tunnel, 'Settings', lambda: settings)
    monkeypatch.setattr(meeting_tunnel, 'ROOT', tmp_path)
    monkeypatch.setattr(meeting_tunnel.shutil, 'which', lambda _: 'ngrok')
    entries = []

    def answer(prompt):
        entries.append(prompt)
        return 'https://meet.google.com/abc-defg-hij'

    monkeypatch.setattr('builtins.input', answer)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(meeting_tunnel.httpx, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(meeting_tunnel.time, 'sleep', lambda _: None)
    return entries


def test_only_url_required_and_meeting_leaves_on_completion(monkeypatch, settings, tmp_path, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'protocol_version': 10,
                'screen_features': {'capture': False, 'vision': False}})
        if request.method == 'POST' and request.url.path == '/api/meetings':
            assert json.loads(request.content)['consent_confirmed'] is True
            return httpx.Response(200, json={'session_id': 'session-test', 'bot_id': 'bot-test'})
        if request.url.path.endswith('/leave'):
            return httpx.Response(200, json={'status': 'left'})
        return httpx.Response(200, json={'status': 'done'})

    entries = configure(monkeypatch, settings, tmp_path, handler)
    assert meeting_tunnel.main() == 0
    assert '[Screen] disabled' in capsys.readouterr().out
    assert len(entries) == 1
    assert sum(r.url.path == '/api/meetings' for r in calls) == 1
    assert any(r.url.path.endswith('/leave') for r in calls)
    assert (tmp_path / 'data/last-meeting.json').exists()


def test_failed_join_does_not_create_duplicate_bot(monkeypatch, settings, tmp_path):
    joins = []

    def handler(request):
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'protocol_version': 10})
        joins.append(request)
        return httpx.Response(503, json={'detail': 'ambiguous creation failure'})

    configure(monkeypatch, settings, tmp_path, handler)
    assert meeting_tunnel.main() == 1
    assert len(joins) == 1
