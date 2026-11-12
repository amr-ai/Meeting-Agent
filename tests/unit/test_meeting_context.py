import json

import httpx
from livekit.agents import llm

from app.meeting.events import EventProcessor
from app.memory.context import context
from app.memory.live import MeetingLink
from app.observability.conversation import ConversationConsole


async def participant_event(db, processor, sid, event, stamp, name='Alice'):
    eid = f'{event}-{stamp}'
    await processor.ingest(eid, sid, {'event': event, 'data': {'bot': {'id': 'bot-1'},
        'data': {'participant': {'id': 7, 'name': name}, 'timestamp': {'relative': stamp}}}})
    await processor.process((await db.rows('SELECT * FROM events WHERE id=?', (eid,)))[0])


async def test_roster_speaker_transitions_and_out_of_order_events(store):
    db, sid = store
    processor = EventProcessor(db)
    await participant_event(db, processor, sid, 'participant_events.join', 1)
    await participant_event(db, processor, sid, 'participant_events.speech_on', 2, name=None)
    data = await db.live_context(sid)
    assert data['participants'][0]['name'] == 'Alice'
    assert data['active_speakers'][0]['id'] == '7'
    await participant_event(db, processor, sid, 'participant_events.speech_off', 4)
    await participant_event(db, processor, sid, 'participant_events.speech_on', 3)
    assert not (await db.live_context(sid))['active_speakers']
    await participant_event(db, processor, sid, 'participant_events.leave', 5)
    assert not (await db.live_context(sid))['participants'][0]['present']


async def test_conversation_preserves_human_and_assistant_and_deduplicates(store):
    db, sid = store
    await db.add_conversation(sid, 'u1', 'user', 'What is the API?')
    await db.add_conversation(sid, 'a1', 'assistant', 'The API is an interface.',
                              'Khaled', source='agent-playout', interrupted=True)
    await db.add_conversation(sid, 'a1', 'assistant', 'duplicate')
    first = await db.live_context(sid)
    assert len(first['messages']) == 2
    assert first['messages'][1]['interrupted']
    assert not (await db.live_context(sid, first['cursor']))['messages']
    recovered = await context(db, sid)
    assert 'The API is an interface.' in recovered and '[interrupted]' in recovered


async def test_incremental_metadata_updates_do_not_replay_native_conversation(settings):
    class Agent:
        chat_ctx = llm.ChatContext()
        updates = 0

        async def update_chat_ctx(self, ctx):
            self.chat_ctx = ctx
            self.updates += 1

    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    agent = Agent()
    async with httpx.AsyncClient() as http:
        link = MeetingLink(agent, http, settings, 'test', Telemetry())
        payload = {'participants': [{'id': '7', 'name': 'Alice', 'present': 1}],
            'active_speakers': [{'id': '7', 'name': 'Alice'}],
            'messages': [{'role': 'assistant', 'text': 'Already answered.', 'interrupted': 0,
                          'source': 'agent-playout'}], 'cursor': 1}
        await link.apply(payload)
        text = agent.chat_ctx.items[-1].text_content
        assert 'Alice' in text and 'not a request to speak' in text
        assert 'Already answered.' not in text
        await link.apply({**payload, 'cursor': 2})
        assert agent.updates == 1
        assert link.cursor == 2


async def test_conversation_journal_contains_no_provider_credentials(settings):
    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    async with httpx.AsyncClient() as http:
        link = MeetingLink(None, http, settings, 'test', Telemetry())
        event = {'event_key': 'u1', 'role': 'user', 'text': 'Hello', 'interrupted': False}
        await link.journal(event)
        saved = link.outbox.read_text(encoding='utf-8')
        assert json.loads(saved) == event
        assert settings.admin_api_key.get_secret_value() not in saved


def test_console_displays_names_and_replies_without_duplicates():
    output = []
    console = ConversationConsole(output.append)
    data = {'participants': [{'id': '7', 'name': 'Alice', 'present': 1}],
        'active_speakers': [{'name': 'Alice'}], 'messages': [
            {'seq': 1, 'role': 'user', 'speaker': 'Alice', 'text': 'Hello',
             'source': 'recall-live', 'interrupted': 0},
            {'seq': 2, 'role': 'assistant', 'speaker': 'Khaled', 'text': 'Hi',
             'source': 'agent-playout', 'interrupted': 1}], 'cursor': 2}
    console.display(data)
    count = len(output)
    console.display(data)
    assert len(output) == count
    assert any('Alice' in line and 'Hello' in line for line in output)
    assert any('Khaled' in line and '[interrupted]' in line for line in output)


async def test_local_conversation_journal_replays_after_worker_restart(settings):
    import asyncio
    calls = []
    received = asyncio.Event()

    async def handler(request):
        calls.append(json.loads(request.content))
        received.set()
        return httpx.Response(200, json={'accepted': True})

    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        old = MeetingLink(None, http, settings, 'test', Telemetry())
        await old.journal({'event_key': 'persisted-before-restart', 'role': 'assistant',
                           'text': 'Earlier reply', 'interrupted': False})
        recovered = MeetingLink(None, http, settings, 'test', Telemetry())
        task = asyncio.create_task(recovered.send())
        await asyncio.wait_for(received.wait(), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert calls[0]['event_key'] == 'persisted-before-restart'
    assert calls[0]['text'] == 'Earlier reply'


async def test_deletion_removes_only_this_meetings_conversation_journals(store):
    db, sid = store
    owned = db.path.parent / f'conversation-{sid}-worker.jsonl'
    unrelated = db.path.parent / 'conversation-unrelated-worker.jsonl'
    owned.write_text('private conversation', encoding='utf-8')
    unrelated.write_text('other meeting', encoding='utf-8')
    await db.cleanup_journals(sid)
    assert not owned.exists()
    assert unrelated.exists()


async def test_long_conversation_items_are_split_without_losing_text(settings):
    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    async with httpx.AsyncClient() as http:
        link = MeetingLink(None, http, settings, 'test', Telemetry())
        original = 'A' * 40001
        link.record('long-turn', 'user', original)
        parts = [link.pending.get_nowait() for _ in range(link.pending.qsize())]
        assert ''.join(p['text'] for p in parts) == original
        assert len({p['event_key'] for p in parts}) == 3


async def test_speaker_silence_flapping_does_not_flood_live_model(settings):
    class Agent:
        chat_ctx = llm.ChatContext()
        updates = 0
        is_speaking = False

        async def update_chat_ctx(self, ctx):
            self.chat_ctx = ctx
            self.updates += 1

    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    async with httpx.AsyncClient() as http:
        agent = Agent()
        link = MeetingLink(agent, http, settings, 'test', Telemetry())
        data = {'participants': [{'id': '7', 'name': 'Alice', 'present': 1}],
                'active_speakers': [{'id': '7', 'name': 'Alice'}], 'messages': [], 'cursor': 0}
        await link.apply(data)
        for _ in range(10):
            await link.apply({**data, 'active_speakers': []})
            await link.apply(data)
        assert agent.updates == 1
        agent.is_speaking = True
        await link.apply({**data, 'participants': [], 'active_speakers': []})
        assert agent.updates == 1
        agent.is_speaking = False
        await link.apply({**data, 'participants': [], 'active_speakers': []})
        assert agent.updates == 2


def test_unattributed_live_text_is_not_displayed_as_an_extra_person():
    output = []
    console = ConversationConsole(output.append)
    console.display({'participants': [{'id': '7', 'name': 'Amr', 'present': 1}],
        'active_speakers': [{'name': 'Amr'}], 'messages': [
            {'seq': 1, 'role': 'user', 'speaker': 'Unknown', 'text': 'Hello',
             'source': 'gemini-live', 'interrupted': 0}], 'cursor': 1})
    assert any('speaker pending Recall' in line and 'Hello' in line for line in output)
    assert not any('[Unknown / live transcript]' in line for line in output)


async def test_local_link_persists_conversation_without_reading_or_updating_model_context(settings):
    import asyncio
    written = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request)
        written.set()
        return httpx.Response(200, json={'accepted': True})

    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    # No agent context methods: a metadata sync would fail this test.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        link = MeetingLink(object(), http, settings, 'local-test', Telemetry())
        link.start(sync_context=False)
        try:
            link.record('turn-1', 'user', 'Hello')
            await asyncio.wait_for(written.wait(), timeout=2)
            assert len(link.tasks) == 1
            assert all(r.method == 'POST' and r.url.path.endswith('/conversation') for r in requests)
        finally:
            await link.close()


async def test_recall_updates_wait_through_thinking_without_losing_annotations(settings):
    class Agent:
        chat_ctx = llm.ChatContext()
        voice_state = 'thinking'
        is_speaking = False
        updates = 0

        async def update_chat_ctx(self, ctx):
            self.chat_ctx = ctx
            self.updates += 1

    class Telemetry:
        def emit(self, *args, **kwargs):
            pass

    async with httpx.AsyncClient() as http:
        agent = Agent()
        link = MeetingLink(agent, http, settings, 'meeting-test', Telemetry())
        data = {'participants': [{'id': '7', 'name': 'Amr', 'present': 1}],
                'active_speakers': [{'id': '7', 'name': 'Amr'}],
                'messages': [{'role': 'user', 'speaker': 'Amr', 'participant_id': '7',
                              'text': 'Earlier question', 'source': 'recall-live'}], 'cursor': 1}
        await link.apply(data)
        assert agent.updates == 0 and link.cursor == 0
        agent.voice_state = 'listening'
        await link.apply(data)
        assert agent.updates == 1 and link.cursor == 1
        assert 'Earlier question' in agent.chat_ctx.items[-1].text_content


def test_console_reports_screen_arrival_and_loss_without_every_frame_spam():
    output = []
    console = ConversationConsole(output.append)
    data = {'participants': [], 'active_speakers': [], 'messages': [], 'cursor': 0,
            'visual_context': {'connected': True, 'vision_enabled': True, 'screens': [
                {'presenter': 'Ahmed', 'fresh': True, 'width': 1280, 'height': 720}]}}
    console.display(data)
    console.display(data)
    assert output.count('[Screen] Receiving: Ahmed (1280x720)') == 1
    console.display({**data, 'visual_context': {'connected': False, 'screens': []}})
    assert '[Screen] No fresh shared-screen frames; waiting for video.' in output
