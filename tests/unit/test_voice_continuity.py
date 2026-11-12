import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents.voice.room_io import room_io as sdk_io
from livekit.plugins.google.realtime.realtime_api import RealtimeSession

from app.agent.visual_replies import VisualReplies
from app.agent.worker import audio_room_options, create_model
from app.voice.input_health import InputHealth
from tests.unit.test_voice_tool_regression import agent_state


async def test_installed_room_io_keeps_session_and_relinks_same_input_after_disconnect():
    linked = []
    room = object.__new__(sdk_io.RoomIO)
    room._options = audio_room_options('test', keep_input_session=True)
    room._participant_identity = 'input-test'
    room._participant_available_fut = asyncio.get_running_loop().create_future()
    kind = next(iter(sdk_io.DEFAULT_PARTICIPANT_KINDS))
    reason = next(iter(sdk_io.DEFAULT_CLOSE_ON_DISCONNECT_REASONS))
    old = SimpleNamespace(identity='input-test', sid='old', name='input', kind=kind, attributes={}, disconnect_reason=reason)
    room._participant_available_fut.set_result(old)
    room._agent_session = SimpleNamespace(_cancel_user_away_timer=lambda: None,
                                          _on_room_io_participant_linked=linked.append)
    room._emit_session_event = lambda *args: None
    room._close_session_atask = room._delete_room_task = None
    room._on_participant_disconnected(old)
    assert not room._participant_available_fut.done() and room._close_session_atask is None
    new = SimpleNamespace(identity='input-test', sid='new', name='input', kind=kind, attributes={})
    room._on_participant_connected(new)
    assert room.linked_participant is new and linked == [new]
    assert audio_room_options('local').close_on_disconnect is True


def test_audio_health_reports_signal_without_muting_or_recording_audio():
    agent, events = agent_state()
    records = []
    monitor = InputHealth(agent, SimpleNamespace(record=lambda *args: records.append(args)))
    monitor.connection(False)
    assert not agent.input_available and not agent.gate.silenced
    monitor.connection(True)
    frame = rtc.AudioFrame(bytes([255, 3]) * 320, 16000, 1, 320)
    monitor.observe(frame)
    assert agent.input_available and monitor.frames == 1
    assert 'worker_input_audio' in events
    assert any('Audio signal detected' in record[2] for record in records)
    assert not agent.gate.silenced and not hasattr(monitor, 'audio')
    count = len(records)
    monitor.browser(b'{"stage":"execute","text":"untrusted instruction"}')
    assert len(records) == count
    monitor.browser(b'{"stage":"published","text":"untrusted instruction"}')
    assert 'untrusted instruction' not in str(records)


async def test_bad_audio_diagnostics_never_prevent_pcm_forwarding(settings, monkeypatch):
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    forwarded = []
    monkeypatch.setattr(RealtimeSession, 'push_audio', lambda self, frame: forwarded.append(frame))
    model = create_model(settings)
    def bad(frame):
        raise ValueError('Synthetic observer failure')
    model.audio_observer = bad
    session = model.session()
    try:
        frame = rtc.AudioFrame.create(16000, 1, 320)
        session.push_audio(frame)
        session.push_audio(frame)
        assert forwarded == [frame, frame]
    finally:
        await session.aclose()
        await model.aclose()


class Speech:
    def __init__(self):
        self.future = asyncio.get_running_loop().create_future()
        self.interrupted = False
    def __await__(self):
        return self.future.__await__()
    def exception(self):
        return self.future.exception()
    def interrupt(self):
        self.interrupted = True
        if not self.future.done():
            self.future.set_result(None)


def visual_setup():
    agent, events = agent_state()
    agent.gate.transcript('Khaled inspect the screen')
    agent.voice_state, agent.input_available = 'listening', True
    speech, calls = Speech(), []
    def generate(**kwargs):
        calls.append(kwargs)
        return speech
    session = SimpleNamespace(user_state='listening', generate_reply=generate)
    return agent, session, speech, calls, VisualReplies(agent, session)


@pytest.mark.parametrize('busy', ['user', 'agent', 'input'])
async def test_visual_answer_waits_for_voice_and_transport_then_uses_public_scheduler(busy):
    agent, session, speech, calls, replies = visual_setup()
    if busy == 'user':
        session.user_state = 'speaking'
    elif busy == 'agent':
        agent.voice_state = 'speaking'
    else:
        agent.input_available = False
    replies.submit('call', 'What is visible?', agent.gate.stop_serial, agent.gate.epoch)
    await asyncio.sleep(.25)
    assert not calls
    session.user_state, agent.voice_state, agent.input_available = 'listening', 'listening', True
    await asyncio.sleep(.35)
    assert len(calls) == 1 and calls[0]['tool_choice'] == 'none' and calls[0]['allow_interruptions']
    speech.future.set_result(None)
    await asyncio.sleep(.05)
    await replies.close()
    assert not speech.interrupted and not replies.tasks


@pytest.mark.parametrize('stop', [True, False])
async def test_stop_or_new_question_interrupts_only_visual_speech_not_the_session(stop):
    agent, session, speech, calls, replies = visual_setup()
    replies.submit('call', 'What is visible?', agent.gate.stop_serial, agent.gate.epoch)
    await asyncio.sleep(.3)
    assert calls
    if stop:
        agent.gate.silence()
    else:
        agent.turns.transcribed(SimpleNamespace(item_id='new', transcript='Khaled another question', is_final=True))
        agent.gate.transcript('Khaled another question')
    await asyncio.sleep(.2)
    assert speech.interrupted and not speech.future.cancelled()
    if not stop:
        assert not agent.gate.silenced and (await agent.gate.claim())[0]
    await replies.close()


async def test_new_visual_request_supersedes_queued_old_snapshot_without_losing_latest():
    agent, session, speech, calls, replies = visual_setup()
    session.user_state = 'speaking'
    replies.submit('old', 'Old snapshot?', agent.gate.stop_serial, agent.gate.epoch)
    await asyncio.sleep(.05)
    replies.submit('new', 'New snapshot?', agent.gate.stop_serial, agent.gate.epoch)
    session.user_state = 'listening'
    await asyncio.sleep(.3)
    assert len(calls) == 1 and 'New snapshot?' in calls[0]['instructions']
    speech.future.set_result(None)
    await replies.close()


async def test_visual_trace_correlates_wait_schedule_and_terminal_without_question_text():
    agent, session, speech, calls, replies = visual_setup()
    rows = []
    agent.telemetry.emit = lambda stage, **data: rows.append((stage, data))
    replies.submit('trace-call', 'private screen question', agent.gate.stop_serial, agent.gate.epoch)
    await asyncio.sleep(.35)
    speech.future.set_result(None)
    await asyncio.sleep(.05)
    await replies.close()
    assert calls
    assert all(row['call_id'] == 'trace-call' for _, row in rows)
    terminal = [row for stage, row in rows if stage == 'visual_reply_terminal']
    assert len(terminal) == 1 and terminal[0]['outcome'] == 'completed'
    scheduled = next(row for stage, row in rows if stage == 'visual_reply_scheduled')
    assert scheduled['wait_ms'] >= 200
    assert 'private screen question' not in str(rows)
