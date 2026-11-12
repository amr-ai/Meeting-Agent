import asyncio
from types import SimpleNamespace

import httpx
import pytest
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import FunctionCall, ToolResult
from livekit.agents.voice.tool_executor import _ToolExecutor

from app.agent.parallel_tools import ParallelTools
from app.meeting.chat.service import ChatService
from app.storage.chat_repository import ChatRepository
from tests.unit.test_chat import Recall
from tests.unit.test_voice_tool_regression import agent_state


def setup():
    agent, events = agent_state()
    agent.gate.transcript('Khaled search')
    agent.parallel = ParallelTools(agent)
    return agent, events


def context(cid, updates):
    async def update(message, **kwargs):
        updates.append((cid, message, kwargs))
    return SimpleNamespace(function_call=SimpleNamespace(call_id=cid), update=update)


def scheduled(*ids):
    return SimpleNamespace(update=SimpleNamespace(type='tool_reply_updated', status='scheduled', update_ids=ids))


async def test_two_tools_release_progress_and_survive_new_question_with_out_of_order_results():
    agent, events = setup()
    updates, releases = [], [asyncio.Event(), asyncio.Event()]

    async def work(index):
        await releases[index].wait()
        return {'result': index}

    tasks = [asyncio.create_task(agent.parallel.run(context(str(i), updates), 'search', 'started',
             lambda i=i: work(i))) for i in range(2)]
    await asyncio.sleep(0)
    assert len(updates) == 2 and not any(t.done() for t in tasks)
    assert not agent.pending_tools
    agent.turns.transcribed(SimpleNamespace(item_id='new-question', transcript='Khaled what is two plus two?'))
    agent.gate.transcript('Khaled what is two plus two?')
    assert (await agent.gate.claim())[0]  # Another spoken answer is allowed while both run.
    releases[1].set()
    assert await tasks[1] == {'result': 1}
    assert not tasks[0].done()
    agent.parallel.on_event(scheduled('1_final'))
    assert (await agent.gate.claim())[0]
    releases[0].set()
    assert await tasks[0] == {'result': 0}
    assert 'parallel_result_reply_scheduled' in events


async def test_stop_keeps_work_running_but_never_restarts_its_old_spoken_result():
    agent, _ = setup()
    release = asyncio.Event()

    async def work():
        await release.wait()
        return ToolResult({'saved': True}, reply_required=False)

    task = asyncio.create_task(agent.parallel.run(context('old', []), 'search', 'started', work))
    await asyncio.sleep(0)
    agent.gate.silence()
    agent.turns.provider_started()
    assert agent.gate.silenced and not task.done()
    release.set()
    result = await task
    assert isinstance(result, ToolResult) and not result.reply_required and result.output == {'saved': True}
    agent.gate.reset()
    agent.gate.transcript('Khaled hello again')
    await agent.gate.claim()
    agent.parallel.on_event(scheduled('old_final'))
    assert agent.gate.claimed  # A new wake must not revive a result suppressed by stop.


def test_unknown_or_unscheduled_provider_updates_cannot_authorize_background_audio():
    agent, _ = setup()
    agent.gate.active_until = 0
    agent.gate.reset()
    agent.gate.transcript('Unrelated meeting speech')
    agent.parallel.on_event(scheduled('untrusted_final'))
    assert not agent.gate.authorized


async def test_cancelled_research_is_not_restarted_or_reserved_for_sending(store, settings):
    db, sid = store
    repo = ChatRepository(db)
    await repo.enqueue_research(sid, 'RAG', 'title and link', 'cancel-test')
    job = (await db.rows('SELECT * FROM chat_voice_jobs'))[0]
    async with httpx.AsyncClient() as http:
        service = ChatService(db, Recall(), http, settings)
        assert (await service.cancel_research(sid, job['id']))['cancelled']

        class Never:
            async def run(self, job):
                pytest.fail('Cancelled job must never run')

        await service.process_research(job, Never())
        with pytest.raises(ValueError):
            await service.cancel_research('other-meeting', job['id'])
        assert (await db.rows('SELECT status FROM chat_voice_jobs'))[0]['status'] == 'cancelled'


async def test_research_jobs_overlap_without_blocking_the_queue(store, settings):
    db, sid = store
    for i in range(2):
        await ChatRepository(db).enqueue_research(sid, f'query {i}', 'title', f'parallel-{i}')
    running, ready = set(), asyncio.Event()
    async with httpx.AsyncClient() as http:
        service = ChatService(db, Recall(), http, settings.model_copy(update={'chat_workers': 2}))

        async def work(job, workflow):
            running.add(job['id'])
            if len(running) == 2:
                ready.set()
            await asyncio.Event().wait()

        service.process_research = work
        runner = asyncio.create_task(service.research_jobs())
        service.tasks.append(runner)
        try:
            await asyncio.wait_for(ready.wait(), timeout=3)
            assert len(service.research_running) == 2
        finally:
            await service.close()


async def test_explicit_sdk_cancellation_releases_only_the_target_task():
    agent, _ = setup()
    async def work():
        await asyncio.Event().wait()
    task = asyncio.create_task(agent.parallel.run(context('cancel', []), 'search', 'started', work))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.parallel.calls['cancel']['status'] == 'cancelled_or_failed'


async def test_installed_sdk_releases_tool_dispatch_on_progress_before_work_finishes():
    # Exercise the real SDK executor/RunContext, with no network or media mocks.
    agent, events = setup()
    release, completed = asyncio.Event(), asyncio.Event()
    class Session:
        _global_run_state = None

        def _tool_execution_updated(self, event):
            events.append(event)

    session = Session()
    speech = SimpleNamespace(num_steps=1, allow_interruptions=True)
    ctx = RunContext(session=session, speech_handle=speech,
        function_call=FunctionCall(call_id='sdk', name='background', arguments='{}'))

    @function_tool
    async def background(context: RunContext):
        """Run a deliberately slow operation."""
        async def work():
            await release.wait()
            completed.set()
            return {'done': True}
        return await agent.parallel.run(context, 'background', 'Started; conversation is available', work)

    executor = _ToolExecutor()
    try:
        result = await asyncio.wait_for(executor.execute(tool=background, run_ctx=ctx, raw_arguments={}), timeout=2)
        assert 'Started' in result and not completed.is_set()
        assert executor.has_running_tasks
        assert ctx.function_call.extra['__livekit_agents_tool_non_blocking']
    finally:
        await executor.aclose()
