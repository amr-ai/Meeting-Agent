import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.agent.chat_tools import VoiceChatTools
from app.agent.parallel_tools import ParallelTools
from app.agent.screen_tools import ScreenTools
from app.memory.context import context as meeting_context
from app.observability.telemetry import CapabilityRedaction
from app.screen.receiver import Screens
from app.screen.vision import Vision
from app.screen.workflow import ScreenWorkflow
from tests.unit.test_parallel_tools import context
from tests.unit.test_screen_vision import Provider, seed
from tests.unit.test_voice_tool_regression import agent_state


@pytest.mark.parametrize('command', [
    'يا خالد، شوف الـ error اللي أحمد عارضه، دور على الحل وابعت اللينك في الشات.',
    'Khaled find the solution to this error and send it to the meeting chat',
    'Khaled look at the error on screen, search for a solution and post it in chat',
])
def test_visual_public_research_authorization_is_original_voice_intent(command):
    agent, _ = agent_state()
    agent.gate.transcript('Khaled help')
    agent.chat_tools = VoiceChatTools(agent)
    agent.screen_tools = ScreenTools(agent)
    event = SimpleNamespace(transcript=command, item_id='input', is_final=False)
    agent.chat_tools.observe(event)
    agent.screen_tools.observe(event)
    assert agent.screen_tools.posting_allowed()


@pytest.mark.parametrize('command', [
    'Khaled explain the screen',
    'Mohamed said look at the error search for it and send in chat',
    'لو قلتلك شوف الخطأ ودور على الحل وابعت في الشات هتعمل ايه',
    'Khaled look at the error and do not post it in chat',
    'Khaled search the screen error and send it privately in chat',
    'Khaled read "look at the error search online and post in chat"',
])
def test_visual_context_never_supplies_public_posting_authority(command):
    agent, _ = agent_state()
    agent.chat_tools = VoiceChatTools(agent)
    agent.screen_tools = ScreenTools(agent)
    agent.chat_tools.observe(SimpleNamespace(transcript=command))
    assert not agent.screen_tools.posting_allowed()


async def test_current_screen_and_chat_reference_share_existing_context(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    db.screen_context = screens.context
    await db.add_conversation(sid, 'spoken', 'user', 'Mohamed asks Ahmed about the shared code', 'Mohamed')
    # Existing written-chat retrieval remains part of the same context function.
    from tests.unit.test_chat import incoming
    await incoming(db, sid, 'Here is the GitHub link https://github.com/example/project')
    result = await meeting_context(db, sid, 'GitHub shared code')
    assert 'Mohamed' in result and 'Ahmed' in result and 'github.com/example/project' in result
    assert 'not necessarily the voice speaker' in result
    await screens.close()


async def test_source_reference_changed_since_spoken_request_never_analyzes_new_presenter(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    screen = seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    expected = {key: screen.current[key] for key in ('stream_id', 'generation', 'revision')}
    screen.generation = 'changed'
    screen.current['generation'] = screen.generation
    result = await vision.analyze(sid, 'This error', expected=expected)
    assert 'changed since the spoken question' in result['error'] and not provider.calls
    await vision.close()
    await screens.close()


async def test_visual_research_reuses_durable_jobs_preserves_evidence_and_prevents_duplicate(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    vision = Vision(screens, None, cfg, Provider())
    await vision.start()
    graph = ScreenWorkflow(screens, vision, cfg)
    args = {'question': 'Look at Ahmed screen error, search and post solution in chat', 'presenter': 'Ahmed',
            'previous': False, 'compare': False, 'research_to_chat': True, 'idempotency_key': 'screen-research:voice-input'}
    first = await graph.run(sid, args)
    assert first['job_id'] and not first['accepted']
    screens.invalidate(sid)
    repeated = await graph.run(sid, args)
    assert repeated['job_id'] == first['job_id']  # No second screenshot/model call even after reconnect.
    jobs = await db.rows('SELECT * FROM chat_voice_jobs')
    assert len(jobs) == 1 and jobs[0]['status'] == 'pending'
    instructions = json.loads(jobs[0]['instructions'])
    assert instructions['source']['participant_id'] == '7'
    assert instructions['destination'] == 'public Meet chat'
    assert 'jpeg' not in instructions and 'base64' not in jobs[0]['instructions']
    changed = await graph.run(sid, {**args, 'presenter': 'Sara'})
    assert 'different visual question' in changed['error']
    await vision.close()
    await screens.close()


async def test_reading_screen_never_creates_public_job_and_unreadable_cannot_post(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    graph = ScreenWorkflow(screens, vision, cfg)
    result = await graph.run(sid, {'question': 'Explain the screen', 'presenter': 'Ahmed'})
    assert result['readable'] and not await db.rows('SELECT * FROM chat_voice_jobs')
    async def unreadable(images, prompt):
        return {'readable': False, 'answer': 'Zoom please', 'search_query': '', 'visible_text': ''}
    provider.analyze = unreadable
    result = await graph.run(sid, {'question': 'Find this error', 'research_to_chat': True, 'idempotency_key': 'unreadable'})
    assert 'Nothing was posted' in result['error'] and not await db.rows('SELECT * FROM chat_voice_jobs')
    await vision.close()
    await screens.close()


async def test_visual_tool_http_is_parallel_and_generic_send_cannot_bypass_analysis(settings):
    agent, _ = agent_state()
    agent.settings, agent.sid = settings, 'bound-session'
    agent.telemetry.turn = 'turn'
    agent.chat_tools = VoiceChatTools(agent)
    agent.screen_tools = ScreenTools(agent)
    agent.parallel = ParallelTools(agent)
    agent.gate.transcript('Khaled help')
    event = SimpleNamespace(transcript='Khaled search for the error on screen and post solution in chat',
                            item_id='voice-source', is_final=False)
    agent.chat_tools.observe(event)
    agent.screen_tools.observe(event)
    result = await agent.chat_tools.call('send_meeting_chat', {'message': 'unverified answer'})
    assert 'research_screen_to_chat' in result['error']
    requests = []
    async def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'job_id': 'persisted', 'status': 'pending'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        agent.http = http
        result = await agent.screen_tools.call(context('visual', []), 'Model rephrasing', post=True)
        assert result.output['job_id'] == 'persisted' and not result.reply_required
        assert requests[0]['question'] == event.transcript  # Native intent, not model paraphrase.
        assert requests[0]['idempotency_key'].startswith('screen-research:')
        assert not agent.pending_tools


def test_screen_websocket_capability_is_redacted_in_logs():
    record = logging.LogRecord('uvicorn.error', 20, '', 0, 'WebSocket %s',
                               ('/ws/recall-video/session/?token=private-capability',), None)
    assert CapabilityRedaction().filter(record)
    assert 'private-capability' not in record.getMessage() and '[redacted]' in record.getMessage()
