import asyncio

import httpx
from livekit import rtc

from app.agent.worker import MeetingAgent
from app.meeting.events import EventProcessor
from app.screen.receiver import Screens
from app.screen.vision import Vision
from tests.unit.test_screen_vision import Provider, seed
from tests.unit.test_voice_tool_regression import agent_state


async def test_voice_frames_and_chat_ingestion_continue_during_slow_vision(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    provider.release.clear()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    pending = asyncio.create_task(vision.analyze(sid, 'Explain current error'))
    await provider.started.wait()
    agent, _ = agent_state()
    agent.gate.transcript('Khaled answer another question')
    agent.published_audio = False
    frame = rtc.AudioFrame.create(24000, 1, 480)
    input_frames = []
    async def audio():
        for index in range(20):
            input_frames.append(index)
            await asyncio.sleep(0)
            yield frame
    output = [item async for item in MeetingAgent.realtime_audio_output_node(agent, audio(), None)]
    assert len(output) == len(input_frames) == 20 and not pending.done()
    processor = EventProcessor(db)
    payload = {'event': 'participant_events.chat_message', 'data': {'bot': {'id': 'bot-1'}, 'data': {
        'participant': {'id': 'mohamed', 'name': 'Mohamed'}, 'data': {'text': 'Chat still arrives'},
        'timestamp': {'relative': 4, 'absolute': '2026-11-09T12:00:00Z'}}}}
    # Use the provider's actual chat text shape shared by the existing parser.
    payload['data']['data']['data'] = {'text': 'Chat still arrives'}
    await processor.ingest('chat-during-vision', sid, payload)
    row = (await db.rows("SELECT * FROM events WHERE id='chat-during-vision'"))[0]
    await processor.process(row)
    assert await db.rows('SELECT * FROM meeting_chat_messages')
    assert not pending.done()
    provider.release.set()
    assert (await pending)['status'] == 'completed'
    await vision.close()
    await screens.close()


async def test_passive_awareness_is_rate_limited_has_no_voice_or_public_side_effect(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True, 'vision_passive_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    for _ in range(100):
        vision.changed(sid, '7')
    await asyncio.gather(*vision.passive)
    assert len(provider.calls) == 1
    assert screens.context(sid)['screens'][0]['cached_summary']['cached_context_only']
    assert not await db.rows('SELECT * FROM chat_voice_jobs')
    assert not await db.rows("SELECT * FROM conversation WHERE role='assistant'")
    for _ in range(100):
        vision.changed(sid, '7')
    assert not vision.passive
    await vision.close()
    await screens.close()


async def test_video_disconnect_cancels_visual_reads_but_preserves_accepted_research(store, settings):
    from app.storage.chat_repository import ChatRepository
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    connection = screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    provider.release.clear()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    accepted = await ChatRepository(db).enqueue_research(sid, 'Python module import error', 'send verified link', 'existing-task')
    pending = asyncio.create_task(vision.analyze(sid, 'Explain'))
    await provider.started.wait()
    screens.disconnect(sid, connection)
    assert (await pending)['status'] == 'cancelled'
    assert (await db.rows('SELECT status FROM chat_voice_jobs WHERE id=?', (accepted['job_id'],)))[0]['status'] == 'pending'
    replacement = screens.connect(sid)
    seed(screens, sid)
    provider.release.set()
    assert (await vision.analyze(sid, 'New screen'))['status'] == 'completed'
    assert screens.connections[sid] == replacement and not vision.task.done()
    await vision.close()
    await screens.close()


async def test_visual_queue_and_completed_results_are_bounded_over_repeated_requests(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    screen = seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    for index in range(80):
        # A continuously arriving static screen remains fresh.
        import time
        screen.current['received_monotonic'] = time.monotonic()
        assert (await vision.analyze(sid, 'Question ' + str(index)))['status'] == 'completed'
    assert len(vision.jobs) == 64
    assert all(job['frame'] is None for job in vision.jobs.values())
    assert len(await db.rows("SELECT * FROM memory WHERE kind='visual_summary'")) == 1
    await vision.close()
    await screens.close()


async def test_local_voice_has_no_visual_tools_even_with_meeting_flags(settings):
    from app.agent.gating import ResponseGate
    from app.observability.telemetry import Telemetry
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as http:
        local = MeetingAgent(cfg, ResponseGate('Khaled', local=True), Telemetry('local', cfg.latency_path), http, 'local', 'local')
        meeting = MeetingAgent(cfg, ResponseGate('Khaled'), Telemetry('meeting', cfg.latency_path), http, 'meeting', 'meeting')
        assert local.screen_tools is None and meeting.screen_tools is not None
        assert len(meeting.screen_tools.functions) == 2
        from livekit.agents.llm import ToolContext
        from livekit.plugins.google.utils import create_tools_config
        declarations, _ = create_tools_config(ToolContext(meeting.tools))
        names = {function.name for tool in declarations for function in tool.function_declarations or []}
        assert {'analyze_shared_screen', 'research_screen_to_chat', 'web_search', 'stop_speaking'} <= names
