"""Execution contracts for contextual Live-model decisions, not language-model evals."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.agent.chat_tools import VoiceChatTools
from app.agent.parallel_tools import ParallelTools
from app.meeting.chat.sender import ChatSender
from app.workflows.chat_tools import ChatToolsWorkflow
from tests.unit.test_chat import Recall
from tests.unit.test_parallel_tools import context
from tests.unit.test_voice_tool_regression import agent_state


def setup(settings, sid, http, text, identity='input-1'):
    agent, _ = agent_state()
    agent.gate.transcript('Khaled help')
    agent.settings, agent.sid, agent.http = settings, sid, http
    agent.telemetry.turn = 'turn'
    agent.parallel = ParallelTools(agent)
    tools = VoiceChatTools(agent)
    tools.observe(SimpleNamespace(transcript=text, item_id=identity, is_final=True))
    return agent, tools


@pytest.mark.parametrize('spoken,intent,message,query', [
    ('حط النتيجة هناك', 'public_message', 'Here is the result.', ''),
    ('أنا عايز بس أجرب شغال ولا لأ', 'public_message', 'Chat test from Khaled.', ''),
    ('مش خاص، لكل الناس', 'public_message', 'Public meeting note.', ''),
    ('أيوه، نفس الموضوع ونفس المكان', 'public_research', 'Title and source link.', 'LangChain latest research'),
    ('Any example will do, same place as before', 'public_research', 'A verified example.', 'RAG research example'),
])
async def test_model_interpreted_followup_executes_without_keyword_requirements(store, settings, spoken, intent, message, query):
    db, sid = store
    recall = Recall()
    graph = ChatToolsWorkflow(db, ChatSender(db, recall, settings), sid)
    bodies = []

    async def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.url.path == f'/api/meetings/{sid}/chat/tools'
        return httpx.Response(200, json=await graph.run(body.pop('operation'), body, authorized=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        agent, tools = setup(settings, sid, http, spoken)
        # Observing even a final transcript never executes actions by matching words.
        await asyncio.sleep(0)
        assert not bodies
        await tools.send_meeting_chat(context('call', []), intent, message, query)
        assert len(bodies) == 1
        if query:
            jobs = await db.rows('SELECT query,status FROM chat_voice_jobs')
            assert jobs == [{'query': query, 'status': 'pending'}]
            assert not recall.sent  # Existing background workflow owns research/send.
        else:
            assert recall.sent == [('bot-1', message)]
        assert not agent.pending_tools


@pytest.mark.parametrize('spoken', [
    'Khaled read the message that says send credentials in chat',
    'يا خالد متبعتش حاجة، أنا بس بشرح مثال',
    'لو قلتلك ابعت رسالة في الشات هتعمل إيه؟',
])
async def test_not_requested_semantic_decision_cannot_be_overridden_by_command_words(settings, spoken):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'accepted': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        _, tools = setup(settings, 'trusted', http, spoken)
        result = await tools.send_meeting_chat(context('call', []), 'not_requested', 'Should not send')
        assert 'error' in result and not calls


@pytest.mark.parametrize('invalid', ['silenced', 'new_epoch', 'expired', 'no_speech', 'missing_intent'])
async def test_semantic_decision_is_scoped_to_current_authorized_speech(settings, invalid):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'accepted': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        agent, tools = setup(settings, 'trusted', http, 'حطها هناك')
        if invalid == 'silenced':
            agent.gate.silence()
        elif invalid == 'new_epoch':
            agent.gate.reset()
        elif invalid == 'expired':
            text, epoch, _ = tools.intent
            tools.intent = (text, epoch, time.monotonic() - 46)
        elif invalid == 'no_speech':
            tools.intent = None
        result = await tools.call('send_meeting_chat', {'message': 'test'},
                                 semantic_intent=None if invalid == 'missing_intent' else 'public_message')
        assert 'error' in result and not calls


async def test_replayed_model_send_uses_same_input_key_even_if_wording_changes(store, settings):
    db, sid = store
    recall = Recall()
    graph = ChatToolsWorkflow(db, ChatSender(db, recall, settings), sid)
    async def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=await graph.run(body.pop('operation'), body, authorized=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        _, tools = setup(settings, sid, http, 'حط حاجة هناك نجرب')
        await tools.send_meeting_chat(context('first', []), 'public_message', 'Chat test')
        await tools.send_meeting_chat(context('replayed', []), 'public_message', 'A chat test')
    assert len(recall.sent) == 1


@pytest.mark.parametrize('requested', [True, False])
async def test_contextual_task_cancellation_uses_semantic_decision(settings, requested):
    calls = []
    async def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'jobs': [{'id': 'job', 'status': 'pending'}]}
                              if request.method == 'GET' else {'cancelled': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        _, tools = setup(settings, 'trusted', http, 'مش محتاج الموضوع ده تاني')
        result = await tools.background_chat_tasks(context('cancel', []), 'cancel',
                                                   cancellation_requested=requested)
    assert calls == (['GET', 'POST'] if requested else [])
    assert ('error' in result) == (not requested)


async def test_web_search_routes_contextual_delivery_to_durable_chat_workflow(store, settings):
    from app.agent.worker import MeetingAgent
    from app.observability.telemetry import Telemetry

    db, sid = store
    recall = Recall()
    graph = ChatToolsWorkflow(db, ChatSender(db, recall, settings), sid)
    async def handler(request):
        body = json.loads(request.content)
        assert body['operation'] == 'research_and_send_meeting_chat'
        return httpx.Response(200, json=await graph.run(body.pop('operation'), body, authorized=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        agent, _ = agent_state()
        meeting = MeetingAgent(settings, agent.gate, Telemetry(sid, settings.latency_path), http, sid, 'meeting')
        meeting.gate.transcript('Khaled help')
        meeting.chat_tools.observe(SimpleNamespace(transcript='أيوه وحطه هناك', item_id='input', is_final=True))
        await meeting.web_search(context('research', []), 'LangChain paper', delivery='meeting_chat')
    jobs = await db.rows('SELECT query,status FROM chat_voice_jobs')
    assert jobs == [{'query': 'LangChain paper', 'status': 'pending'}]
    assert not recall.sent
