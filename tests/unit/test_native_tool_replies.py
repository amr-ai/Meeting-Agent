import asyncio
from types import SimpleNamespace

import pytest
from google.genai import types
from livekit.agents import RunContext, function_tool, llm
from livekit.agents.voice.tool_executor import _ToolExecutor
from livekit.plugins.google.realtime.realtime_api import RealtimeSession

from app.agent.parallel_tools import ParallelTools
from app.agent.worker import create_model
from app.voice.native_tools import NATIVE_TOOL_REPLY, native_tool_context
from tests.unit.test_voice_tool_regression import agent_state


def history(reply=True):
    binding = {'root': 'provider-call', 'reply': reply}
    return llm.ChatContext([
        llm.FunctionCall(name='analyze_shared_screen', call_id='provider-call', arguments='{}',
                         extra={NATIVE_TOOL_REPLY: binding}),
        llm.FunctionCallOutput(name='analyze_shared_screen', call_id='provider-call', output='Still checking', is_error=False),
        llm.FunctionCall(name='analyze_shared_screen', call_id='provider-call_final', arguments='{}',
                         extra={NATIVE_TOOL_REPLY: binding}),
        llm.FunctionCallOutput(name='analyze_shared_screen', call_id='provider-call_final',
                               output='A blue rectangle is visible', reply_required=False, is_error=False),
    ])


def test_native_history_keeps_one_server_call_and_one_real_result():
    ctx = history()
    normalized = native_tool_context(ctx)
    assert len(normalized.items) == 2
    call, result = normalized.items
    assert call.call_id == result.call_id == 'provider-call'
    assert result.reply_required and 'blue rectangle' in result.output
    assert ctx.items[-1].call_id.endswith('_final') and not ctx.items[-1].reply_required
    assert native_tool_context(normalized).items == normalized.items


async def test_actual_pinned_plugin_sends_final_result_to_original_call_and_preserves_replay(settings, monkeypatch):
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    model = create_model(settings)
    session = model.session()
    sent = []
    monkeypatch.setattr(session, '_send_client_event', sent.append)
    try:
        ctx = history()
        binding = ctx.items[0].extra.pop(NATIVE_TOOL_REPLY)
        session._open_call_ids.add('provider-call')
        assert not model.register_native_tool('never-issued', binding)
        assert model.register_native_tool('provider-call', binding)
        # AgentActivity appends its first progress directly to the model context;
        # the model's FunctionCall has no executor metadata until our binding.
        session._sync_chat_ctx(llm.ChatContext(ctx.items[:2]))
        assert not any(isinstance(msg, types.LiveClientToolResponse) for msg in sent)
        session._sync_chat_ctx(ctx)
        replies = [msg for msg in sent if isinstance(msg, types.LiveClientToolResponse)]
        assert len(replies) == 1
        response = replies[0].function_responses[0]
        assert response.id == 'provider-call'
        assert 'blue rectangle' in response.response['output']
        assert response.scheduling != types.FunctionResponseScheduling.SILENT
        assert len(session.chat_ctx.items) == 2  # Fresh reconnect replays normalized history too.
        assert not session._native_tool_bindings
        sent.clear()
        session._sync_chat_ctx(ctx)
        assert not any(isinstance(msg, types.LiveClientToolResponse) for msg in sent)
    finally:
        await session.aclose()
        await model.aclose()


def test_explicit_stop_keeps_native_result_silent():
    ctx = native_tool_context(history(reply=False))
    assert not ctx.items[-1].reply_required


async def test_real_sdk_async_executor_releases_work_without_synthetic_voice_reply():
    agent, events = agent_state()
    agent.gate.transcript('Khaled describe the screen')
    agent.native_tool_replies = True
    agent.parallel = ParallelTools(agent)
    release, finished = asyncio.Event(), asyncio.Event()
    source = llm.FunctionCall(call_id='provider-call', name='analyze_shared_screen', arguments='{}')
    class Target:
        chat_ctx = llm.ChatContext([source])
        async def update_chat_ctx(self, ctx, **kwargs):
            self.chat_ctx = ctx
            finished.set()
    class Session:
        _global_run_state = None
        current_agent = Target()
        history = llm.ChatContext()
        def _tool_execution_updated(self, event):
            pass
        def generate_reply(self, **kwargs):
            raise AssertionError('Native final must not trigger the SDK synthetic reply scheduler')
    session = Session()
    ctx = RunContext(session=session, speech_handle=SimpleNamespace(num_steps=1, allow_interruptions=True),
                     function_call=source)
    @function_tool
    async def analyze_shared_screen(context: RunContext):
        """Inspect a synthetic screen."""
        async def work():
            await release.wait()
            return {'answer': 'A blue rectangle is visible'}
        return await agent.parallel.run(context, 'analyze_shared_screen', 'Checking', work, native_reply=True)
    executor = _ToolExecutor()
    try:
        await asyncio.wait_for(executor.execute(tool=analyze_shared_screen, run_ctx=ctx, raw_arguments={}), 2)
        assert executor.has_running_tasks and not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        normalized = native_tool_context(session.current_agent.chat_ctx)
        assert normalized.items[-1].call_id == 'provider-call'
        assert normalized.items[-1].reply_required
        assert 'blue rectangle' in normalized.items[-1].output
        assert 'native_tool_reply_ready' in events
    finally:
        await executor.aclose()


async def test_native_result_does_not_restart_voice_after_explicit_stop():
    agent, _ = agent_state()
    agent.gate.transcript('Khaled inspect the screen')
    agent.native_tool_replies = True
    agent.parallel = ParallelTools(agent)
    source = llm.FunctionCall(name='analyze_shared_screen', call_id='stopped', arguments='{}')
    progress, release = asyncio.Event(), asyncio.Event()
    async def update(*args, **kwargs):
        progress.set()
    async def work():
        await release.wait()
        return {'answer': 'A blue rectangle'}
    context = SimpleNamespace(function_call=source, update=update)
    pending = asyncio.create_task(agent.parallel.run(context, source.name, 'Checking', work, native_reply=True))
    await progress.wait()
    agent.gate.silence()
    release.set()
    result = await pending
    assert not result.reply_required and agent.gate.silenced
    assert not source.extra[NATIVE_TOOL_REPLY]['reply']


async def test_unexpected_native_tool_error_can_be_explained_instead_of_silent_failure():
    agent, _ = agent_state()
    agent.gate.transcript('Khaled inspect the screen')
    agent.native_tool_replies = True
    agent.parallel = ParallelTools(agent)
    source = llm.FunctionCall(name='analyze_shared_screen', call_id='failed', arguments='{}')
    async def update(*args, **kwargs):
        pass
    async def work():
        raise RuntimeError('Synthetic unavailable provider')
    context = SimpleNamespace(function_call=source, update=update)
    with pytest.raises(RuntimeError):
        await agent.parallel.run(context, source.name, 'Checking', work, native_reply=True)
    assert source.extra[NATIVE_TOOL_REPLY]['reply']
    assert agent.gate.tools_allowed() and not agent.gate.silenced


async def test_native_capture_returns_fresh_snapshot_without_separate_image_model(store, settings):
    from app.screen.receiver import Screens
    from app.screen.workflow import ScreenWorkflow
    from tests.unit.test_screen_vision import seed
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    screen = seed(screens, sid)
    try:
        workflow = ScreenWorkflow(screens, None, cfg)
        expected = {key: screen.current[key] for key in ('stream_id', 'generation', 'revision')}
        expected['revision'] -= 1  # Current questions select fresh content, not stale speech-time pixels.
        result = await workflow.capture(sid, {'question': 'What is visible?', 'expected': expected})
        assert result['image'] and result['mime_type'] == 'image/jpeg'
        assert result['source']['presenter'] == 'Ahmed'
        expected['generation'] = 'different-share'
        rejected = await workflow.capture(sid, {'question': 'What is visible?', 'expected': expected})
        assert 'changed since the question' in rejected['error']
        assert 'error' in await workflow.capture(sid, {'research_to_chat': True})
    finally:
        await screens.close()


async def test_native_image_reaches_installed_google_formatter(settings, monkeypatch):
    from app.screen.vision import GeminiVision
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    model = create_model(settings)
    session = model.session()
    sent = []
    monkeypatch.setattr(session, '_send_client_event', sent.append)
    try:
        import base64
        image = GeminiVision.probe_image()
        ctx = llm.ChatContext()
        ctx.add_message(role='user', content=['Pending screen reference',
            llm.ImageContent(image='data:image/jpeg;base64,' + base64.b64encode(image).decode())])
        session._sync_chat_ctx(ctx)
        parts = [part for msg in sent if isinstance(msg, types.LiveClientContent)
                 for turn in msg.turns for part in turn.parts]
        assert any(part.inline_data and part.inline_data.mime_type == 'image/jpeg' for part in parts)
        assert all(msg.turn_complete is False for msg in sent if isinstance(msg, types.LiveClientContent))
    finally:
        await session.aclose()
        await model.aclose()


async def test_native_screen_tool_uploads_one_requested_image_and_allows_next_question(settings):
    import base64

    import httpx

    from app.agent.screen_tools import ScreenTools
    from app.screen.vision import GeminiVision
    from tests.unit.test_parallel_tools import context
    agent, _ = agent_state()
    agent.gate.transcript('Khaled describe the screen')
    agent.settings, agent.sid = settings, 'bound-meeting'
    agent.native_tool_replies = True
    agent.parallel = ParallelTools(agent)
    images = []
    agent.native_tool_model = SimpleNamespace(register_native_tool=lambda *args: True,
        push_screen_image=lambda call_id, image, question: images.append((call_id, image)) or True)
    requests = []
    async def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json={'image': base64.b64encode(GeminiVision.probe_image()).decode(),
            'source': {'id': 'frame', 'presenter': 'Amr'}, 'mime_type': 'image/jpeg'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        agent.http = http
        result = await ScreenTools(agent).call(context('image-call', []), 'What is on the screen?')
    assert len(requests) == 1 and requests[0].url.path.endswith('/screen-frame')
    assert result.output['image_delivered'] and 'image' not in result.output
    assert len(images) == 1 and images[0][0] == 'image-call'
    agent.turns.transcribed(SimpleNamespace(item_id='next-question', transcript='What is two plus two?', is_final=True))
    assert agent.gate.transcript('What is two plus two?')
    assert (await agent.gate.claim())[0]


async def test_requested_snapshot_uses_live_video_channel_before_result(settings, monkeypatch):
    import base64

    from app.screen.vision import GeminiVision
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    model = create_model(settings)
    session = model.session()
    messages = []
    monkeypatch.setattr(session, '_send_client_event', messages.append)
    try:
        encoded = base64.b64encode(GeminiVision.probe_image()).decode()
        assert not model.push_screen_image('unrequested', encoded)
        session._open_call_ids.add('requested')
        assert model.register_native_tool('requested', {'root': 'requested', 'reply': False})
        assert model.push_screen_image('requested', encoded)
        assert len(messages) == 1 and isinstance(messages[0], types.LiveClientRealtimeInput)
        assert messages[0].video.mime_type == 'image/jpeg'
        assert messages[0].video.data.startswith(b'\xff\xd8')
    finally:
        await session.aclose()
        await model.aclose()


@pytest.mark.parametrize('reply', [True, False])
async def test_visual_answer_turn_follows_frame_and_silent_completion_once(settings, monkeypatch, reply):
    import base64

    from app.screen.vision import GeminiVision
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    model = create_model(settings)
    session = model.session()
    messages, scheduled = [], []
    model.visual_reply_handler = lambda *args: scheduled.append(args)
    monkeypatch.setattr(session, '_send_client_event', messages.append)
    try:
        ctx = history(reply=reply)
        binding = ctx.items[0].extra[NATIVE_TOOL_REPLY]
        session._open_call_ids.add('provider-call')
        model.register_native_tool('provider-call', binding)
        model.push_screen_image('provider-call', base64.b64encode(GeminiVision.probe_image()).decode(), 'What is visible?')
        session._sync_chat_ctx(ctx)
        assert isinstance(messages[0], types.LiveClientRealtimeInput)
        tool_index = next(i for i, msg in enumerate(messages) if isinstance(msg, types.LiveClientToolResponse))
        assert messages[tool_index].function_responses[0].scheduling == types.FunctionResponseScheduling.SILENT
        turns = [(i, msg) for i, msg in enumerate(messages) if isinstance(msg, types.LiveClientContent) and msg.turn_complete]
        assert not turns  # The transport no longer forces an interrupting turn.
        assert len(scheduled) == int(reply)
        if reply:
            assert scheduled[0][1] == 'What is visible?'
        messages.clear()
        session._sync_chat_ctx(ctx)
        assert not messages
        assert len(scheduled) == int(reply)
    finally:
        await session.aclose()
        await model.aclose()
