import asyncio
import time
from types import SimpleNamespace

import httpx

from app.agent.chat_tools import VoiceChatTools
from app.agent.gating import ResponseGate
from app.agent.turns import VoiceTurns
from app.meeting.chat.service import ChatService
from app.storage.chat_repository import ChatRepository
from app.workflows.chat_tools import ChatToolsWorkflow
from tests.unit.test_chat import Recall


def agent_state():
    events = []
    gate = ResponseGate('Khaled,خالد', followup_seconds=30, decision_timeout=.02)
    agent = SimpleNamespace(gate=gate, pending_tools=set(), telemetry=SimpleNamespace(
        emit=lambda stage, **kwargs: events.append(stage), begin_turn=lambda: events.append('begin')))
    agent.turns = VoiceTurns(agent)
    return agent, events


async def test_recorded_tool_result_sequence_does_not_reset_authorization_or_wait_five_seconds():
    agent, events = agent_state()
    gate, turns = agent.gate, agent.turns
    turns.provider_started()
    turns.transcribed(SimpleNamespace(item_id='spoken-question', transcript='Khaled search for this'))
    gate.transcript('Khaled search for this', final=False)
    epoch = gate.epoch
    turns.tool_started()
    gate.speaker_observed = time.monotonic() - 20
    turns.tool_completed(epoch)
    # The actual failing trace emitted two content-free/provider generations
    # after tool completion, with no new input transcript.
    turns.provider_started()
    turns.provider_started()
    assert gate.epoch == epoch
    assert gate.tools_allowed()
    async with asyncio.timeout(.1):
        assert (await gate.claim())[0]
    assert 'tool_continuation_preserved' in events


async def test_actual_new_transcribed_input_revokes_old_tool_chain_and_cancels_pending_work():
    agent, _ = agent_state()
    gate, turns = agent.gate, agent.turns
    turns.provider_started()
    turns.transcribed(SimpleNamespace(item_id='old', transcript='Khaled help'))
    gate.transcript('Khaled help')
    epoch = gate.epoch
    waiting = asyncio.create_task(asyncio.Event().wait())
    agent.pending_tools.add(waiting)
    turns.tool_started()
    turns.provider_started()
    assert not waiting.cancelled() and gate.epoch == epoch
    turns.transcribed(SimpleNamespace(item_id='new-user-input', transcript='Other human conversation'))
    gate.transcript('Other human conversation')
    await asyncio.gather(waiting, return_exceptions=True)
    assert waiting.cancelled() and gate.epoch != epoch
    assert not gate.rearm_tool_reply(epoch)


async def test_stop_can_never_be_undone_by_tool_completion():
    agent, _ = agent_state()
    agent.gate.transcript('Khaled help')
    epoch = agent.gate.epoch
    agent.turns.tool_started()
    agent.gate.silence()
    agent.turns.tool_completed(epoch)
    assert not agent.gate.tools_allowed()
    assert not (await agent.gate.claim())[0]


async def test_empty_provider_generation_cannot_consume_the_pending_tool_reply():
    from livekit import rtc

    from app.agent.worker import MeetingAgent
    agent, _ = agent_state()
    agent.published_audio = False
    gate, turns = agent.gate, agent.turns
    turns.provider_started()
    turns.transcribed(SimpleNamespace(item_id='question', transcript='Khaled search'))
    gate.transcript('Khaled search')
    turns.tool_started()
    turns.tool_completed(gate.epoch)
    turns.provider_started()

    async def empty():
        if False:
            yield

    assert [f async for f in MeetingAgent.realtime_audio_output_node(agent, empty(), None)] == []
    assert turns.awaiting_tool_reply
    turns.provider_started()

    async def answer():
        yield rtc.AudioFrame.create(24000, 1, 480)

    assert len([f async for f in MeetingAgent.realtime_audio_output_node(agent, answer(), None)]) == 1


async def test_rejected_send_can_be_explained_aloud_instead_of_muting_the_error(settings):
    agent, _ = agent_state()
    agent.gate.transcript('Khaled read chat')
    agent.turns.tool_started()
    tools = VoiceChatTools(agent)
    tools.observe(SimpleNamespace(transcript='Khaled read chat'))
    agent.settings = settings
    result = await tools.call('send_meeting_chat', {'message': 'Not authorized'})
    assert 'error' in result
    agent.turns.provider_started()
    assert (await agent.gate.claim())[0]


async def test_research_request_returns_before_search_starts_and_survives_restart(store, settings):
    db, sid = store
    provider = Recall()
    from app.meeting.chat.sender import ChatSender
    graph = ChatToolsWorkflow(db, ChatSender(db, provider, settings), sid)
    result = await graph.run('research_and_send_meeting_chat',
        {'query': 'latest RAG paper', 'message': 'title and link', 'idempotency_key': 'voice-request'}, authorized=True)
    assert result['status'] == 'pending' and not result['accepted'] and not provider.sent
    repeated = await graph.run('research_and_send_meeting_chat',
        {'query': 'latest RAG paper', 'message': 'title and link', 'idempotency_key': 'voice-request'}, authorized=True)
    assert repeated['job_id'] == result['job_id']
    await db.execute("UPDATE chat_voice_jobs SET status='running'")
    await ChatRepository(db).recover()
    assert (await db.rows('SELECT status FROM chat_voice_jobs'))[0]['status'] == 'pending'


async def test_background_research_searches_composes_and_posts_through_existing_sender(store, settings):
    db, sid = store
    provider = Recall()

    class Search:
        async def run(self, query):
            return {'sources': [{'title': 'Verified paper', 'url': 'https://example.com/paper', 'snippet': 'A dated paper'}]}

    class Text:
        async def generate(self, message, context, sources):
            assert sources[0]['title'] == 'Verified paper'
            return 'Verified paper: https://example.com/paper'

    async with httpx.AsyncClient() as http:
        service = ChatService(db, provider, http, settings, Text())
        service.workflow.search = Search()
        await ChatRepository(db).enqueue_research(sid, 'latest RAG paper', 'title and link', 'request')
        job = (await db.rows('SELECT * FROM chat_voice_jobs'))[0]
        await service.process_research(job)
        assert (await db.rows('SELECT status FROM chat_voice_jobs'))[0]['status'] == 'done'
        assert provider.sent == [('bot-1', 'Verified paper: https://example.com/paper')]
        # Replay after a crash with the cached answer cannot send again.
        job = (await db.rows('SELECT * FROM chat_voice_jobs'))[0]
        await service.process_research(job)
        assert len(provider.sent) == 1
