import asyncio
import json
import random
from array import array
from types import SimpleNamespace

import httpx
from livekit import rtc

from app.memory.context import context
from app.memory.live import MeetingLink
from app.voice.input_filter import SpeechInputFilter
from tests.unit.test_voice_tool_regression import agent_state


def utterance(key, text='Khaled, help me', final=True):
    return SimpleNamespace(item_id=key, transcript=text, is_final=final)


async def test_noise_after_answer_never_mints_another_response():
    agent, _ = agent_state()
    assert agent.turns.transcribed(utterance('question'))
    agent.gate.transcript('Khaled, help me')
    assert (await agent.gate.claim())[0]
    epoch = agent.gate.epoch
    for _ in range(3):
        agent.turns.provider_started()
        assert not agent.turns.transcribed(utterance('noise', '...'))
        assert not (await agent.gate.claim())[0]
    assert agent.gate.epoch == epoch
    # Silence doesn't deactivate the caller; real speech still works immediately.
    assert agent.turns.transcribed(utterance('followup', 'Explain more'))
    agent.gate.transcript('Explain more')
    assert (await agent.gate.claim())[0]


async def test_replayed_final_does_not_repeat_answer_but_new_request_can():
    agent, _ = agent_state()
    event = utterance('question')
    assert agent.turns.transcribed(event)
    agent.gate.transcript(event.transcript)
    assert (await agent.gate.claim())[0]
    agent.turns.provider_started()
    assert not agent.turns.transcribed(event)
    assert not agent.turns.transcribed(utterance('question', 'Khaled', False))
    assert not (await agent.gate.claim())[0]
    assert agent.turns.transcribed(utterance('new-question'))
    agent.gate.transcript(event.transcript)
    assert (await agent.gate.claim())[0]


async def test_waiting_audio_accepts_real_transcript_without_waiting_for_recall():
    agent, _ = agent_state()
    agent.turns.provider_started()
    waiting = asyncio.create_task(agent.gate.claim())
    await asyncio.sleep(0)
    agent.turns.transcribed(utterance('real'))
    agent.gate.transcript('Khaled, help me', final=False)
    assert (await waiting)[0]


def test_noise_hint_does_not_cancel_work_or_revoke_playing_audio():
    agent, _ = agent_state()
    agent.turns.transcribed(utterance('real'))
    agent.gate.transcript('Khaled, help me')
    epoch = agent.gate.epoch
    agent.turns.provider_started()
    assert agent.gate.epoch == epoch and agent.gate.tools_allowed()


def test_native_denoiser_preserves_duration_and_reduces_stationary_noise():
    rng = random.Random(9)
    denoiser = SpeechInputFilter()
    assert not denoiser.failed
    before, after = 0, 0
    for index in range(120):
        pcm = array('h', [rng.randint(-600, 600) for _ in range(320)])
        frame = rtc.AudioFrame(pcm.tobytes(), 16000, 1, 320)
        cleaned = denoiser.process(frame)
        assert cleaned.samples_per_channel == 320 and cleaned.sample_rate == 16000
        assert bytes(frame.data) == pcm.tobytes()  # No mutation of shared RTC input.
        if index > 50:
            before += sum(x * x for x in pcm)
            after += sum(x * x for x in cleaned.data)
    assert not denoiser.failed
    assert after < before * .75


def test_denoiser_failure_keeps_voice_available():
    denoiser = SpeechInputFilter()
    def fail(_):
        raise RuntimeError('synthetic failure')
    denoiser.processor = SimpleNamespace(process_stream=fail)
    frame = rtc.AudioFrame.create(16000, 1, 320)
    assert denoiser.process(frame) is frame
    assert denoiser.process(frame) is frame and denoiser.failed


async def test_api_outage_does_not_block_durable_memory_and_replay(settings):
    requested = asyncio.Event()
    async def offline(request):
        requested.set()
        await asyncio.Event().wait()
    telemetry = SimpleNamespace(emit=lambda *args, **kwargs: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
        link = MeetingLink(None, http, settings, 'memory-test', telemetry)
        link.start(sync_context=False)
        try:
            link.record('first', 'user', 'First question')
            await asyncio.wait_for(requested.wait(), 3)
            link.record('second', 'assistant', 'Second answer', speaker='Mariam')
            link.record('third', 'user', 'Another question')
            await asyncio.wait_for(link.pending.join(), 3)
            saved = [json.loads(line) for line in link.outbox.read_text(encoding='utf-8').splitlines()]
            assert [row['event_key'] for row in saved] == ['first', 'second', 'third']
            assert saved[1]['speaker'] == 'Mariam'
            assert all(row['observed_at'] > 0 for row in saved)
        finally:
            await link.close()
    delivered = []
    complete = asyncio.Event()
    async def online(request):
        delivered.append(json.loads(request.content))
        if len(delivered) == 3:
            complete.set()
        return httpx.Response(200, json={'accepted': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(online)) as http:
        recovered = MeetingLink(None, http, settings, 'memory-test', telemetry)
        recovered.start(sync_context=False)
        try:
            await asyncio.wait_for(complete.wait(), 3)
            assert [row['event_key'] for row in delivered] == ['first', 'second', 'third']
        finally:
            await recovered.close()


async def test_memory_does_not_present_recall_mirror_as_a_second_utterance(store):
    from app.meeting.events import parse_utterance
    from tests.unit.test_storage import data
    db, sid = store
    item = parse_utterance(sid, 'bot-1', data('Approved launch date', 'Alice'), 'event')
    await db.upsert_utterance(item)
    await db.add_conversation(sid, 'recall:7:1.0', 'user', item.text,
                              speaker='Alice', participant_id='7', source='recall-live')
    await db.add_conversation(sid, 'answer', 'assistant', 'I recorded the date.', speaker='Khaled')
    result = await context(db, sid)
    assert result.count('Approved launch date') == 1
    assert 'I recorded the date.' in result


async def test_stop_revokes_audio_already_waiting_even_if_new_caller_wakes():
    agent, _ = agent_state()
    waiting = asyncio.create_task(agent.gate.claim())
    await asyncio.sleep(0)
    agent.gate.silence()
    agent.gate.transcript('Khaled, new question')
    assert not (await waiting)[0]
    assert (await agent.gate.claim())[0]


def test_replayed_conversation_keeps_original_observation_time(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.integration.test_api import FakeLiveKit, FakeRecall
    with TestClient(create_app(settings, FakeLiveKit(), FakeRecall())) as client:
        auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
        sid = client.post('/api/local', headers=auth).json()['session_id']
        event = {'event_key': 'outage-message', 'role': 'assistant', 'speaker': 'Mariam',
                 'text': 'Recorded while offline', 'observed_at': 1700000000.25}
        url = f'/api/meetings/{sid}/conversation'
        assert client.post(url, headers=auth, json=event).status_code == 200
        assert client.post(url, headers=auth, json=event).status_code == 200
        messages = client.get(f'/api/meetings/{sid}/live-context', headers=auth).json()['messages']
        matching = [m for m in messages if m['event_key'] == 'outage-message']
        assert len(matching) == 1
        assert matching[0]['created'] == event['observed_at']
        assert matching[0]['speaker'] == 'Mariam'
