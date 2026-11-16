import asyncio
from types import SimpleNamespace

from app.agent.parallel_tools import ParallelTools
from tests.unit.test_parallel_tools import scheduled
from tests.unit.test_voice_noise_memory import utterance
from tests.unit.test_voice_tool_regression import agent_state


async def test_late_final_is_archived_without_replacing_newer_question():
    agent, _ = agent_state()
    saved = []
    agent.meeting_link = SimpleNamespace(record=lambda *args, **kwargs: saved.append(args))
    turns = agent.turns
    assert turns.transcribed(utterance('A', 'Khaled, first question', False))
    agent.gate.transcript('Khaled, first question', final=False)
    assert turns.transcribed(utterance('B', 'Khaled, second question', False))
    agent.gate.transcript('Khaled, second question', final=False)
    epoch = agent.gate.epoch
    pending = asyncio.create_task(asyncio.Event().wait())
    agent.pending_tools.add(pending)
    try:
        assert not turns.transcribed(utterance('A', 'Khaled, first question complete'))
        assert turns.input_id == 'B' and agent.gate.epoch == epoch
        assert turns.last_input_text == 'Khaled, second question'
        assert not pending.cancelling()
        assert saved == [('A', 'user', 'Khaled, first question complete')]
        assert not turns.transcribed(utterance('A', 'Khaled, first question complete'))
        assert len(saved) == 1
        assert turns.transcribed(utterance('B', 'Khaled, second question complete'))
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def test_speech_end_without_transcript_closes_candidate_not_conversation():
    agent, _ = agent_state()
    agent.gate.transcript('Khaled, hello')
    agent.turns.provider_started()
    assert agent.turns.awaiting_transcript
    agent.turns.provider_stopped()
    assert not agent.turns.awaiting_transcript
    assert not agent.gate.silenced
    assert agent.gate.followup_allowed()


def test_missing_speech_end_has_bounded_candidate_lifetime(monkeypatch):
    import app.agent.turns as module
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    agent, _ = agent_state()
    agent.turns.provider_started()
    clock[0] += 10
    assert not agent.turns.awaiting_transcript
    assert not agent.gate.silenced


async def test_same_tool_update_can_authorize_only_one_reply():
    agent, _ = agent_state()
    agent.gate.transcript('Khaled, search')
    parallel = ParallelTools(agent)
    parallel.calls['job'] = {'status': 'done', 'stop_serial': agent.gate.stop_serial}
    parallel.on_event(scheduled('job_final'))
    assert (await agent.gate.claim())[0]
    serial = agent.turns.tool_serial
    parallel.on_event(scheduled('job_final'))
    assert not (await agent.gate.claim())[0]
    assert agent.turns.tool_serial == serial


async def test_reordered_coalesced_results_allow_only_new_updates():
    agent, _ = agent_state()
    parallel = ParallelTools(agent)
    for key in ('A', 'B'):
        parallel.calls[key] = {'status': 'done', 'stop_serial': agent.gate.stop_serial}
    parallel.on_event(scheduled('A_final'))
    assert (await agent.gate.claim())[0]
    parallel.on_event(scheduled('B_final', 'A_final'))
    assert (await agent.gate.claim())[0]  # B really is new.
    parallel.on_event(scheduled('A_final', 'B_final'))
    assert not (await agent.gate.claim())[0]
    parallel.on_event(scheduled('A_unrecognized_suffix'))
    assert not (await agent.gate.claim())[0]


def test_completed_tool_history_is_bounded_without_losing_active_calls():
    agent, _ = agent_state()
    parallel = ParallelTools(agent)
    parallel.calls['active'] = {'status': 'running'}
    for index in range(1000):
        parallel.calls[str(index)] = {'status': 'done'}
    parallel.prune()
    assert len(parallel.calls) == 257
    assert 'active' in parallel.calls and '999' in parallel.calls and '0' not in parallel.calls


async def test_old_long_running_tool_keeps_its_fresh_completion_permit():
    from tests.unit.test_parallel_tools import context, setup
    agent, _ = setup()
    parallel = agent.parallel
    release = asyncio.Event()
    async def work():
        await release.wait()
        return {'result': 'ready'}
    task = asyncio.create_task(parallel.run(context('slow', []), 'search', 'working', work))
    await asyncio.sleep(0)
    for index in range(300):
        parallel.calls[str(index)] = {'status': 'done', 'completion_serial': index,
                                      'stop_serial': agent.gate.stop_serial}
    parallel.completion_serial = 300
    release.set()
    await task
    assert 'slow' in parallel.calls and len(parallel.calls) == 256
    parallel.on_event(scheduled('slow_final'))
    assert (await agent.gate.claim())[0]


async def test_noise_candidate_cannot_block_visual_reply_until_30_second_deadline():
    from tests.unit.test_voice_continuity import visual_setup
    agent, session, speech, calls, replies = visual_setup()
    agent.turns.candidate_timeout = .01
    agent.turns.provider_started()
    try:
        replies.submit('screen', 'What is here?', agent.gate.stop_serial, agent.gate.epoch)
        await asyncio.sleep(.4)
        assert len(calls) == 1
        assert not agent.turns.awaiting_transcript and not agent.gate.silenced
        speech.future.set_result(None)
    finally:
        await replies.close()


def test_stale_named_final_cannot_wake_stopped_agent():
    agent, _ = agent_state()
    turns = agent.turns
    turns.transcribed(utterance('old', 'Khaled hello', False))
    turns.transcribed(utterance('new', 'Khaled be quiet', False))
    agent.gate.silence()
    assert not turns.transcribed(utterance('old', 'Khaled hello'))
    assert agent.gate.silenced and turns.input_id == 'new'


def test_revision_changes_with_source_but_not_credentials_or_transcripts(tmp_path):
    from app.observability.telemetry import runtime_revision
    (tmp_path / 'app').mkdir()
    source = tmp_path / 'app/test.py'
    source.write_text('x=1\n')
    (tmp_path / 'requirements.lock').write_text('')
    (tmp_path / 'pyproject.toml').write_text('')
    first = runtime_revision(tmp_path)
    assert len(first) == 20
    (tmp_path / '.env').write_text('PRIVATE_KEY=do-not-log-this')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/transcript.txt').write_text('private speech')
    assert runtime_revision(tmp_path) == first
    source.write_text('x=2\n')
    assert runtime_revision(tmp_path) != first


async def test_gate_explains_rejection_without_transcript_content():
    agent, _ = agent_state()
    gate = agent.gate
    gate.transcript('ordinary background conversation')
    assert not (await gate.claim())[0]
    assert gate.last_claim_reason == 'not_addressed'
    gate.transcript('Khaled hello')
    assert (await gate.claim())[0]
    assert gate.last_claim_reason == 'authorized'
    assert not (await gate.claim())[0]
    assert gate.last_claim_reason == 'permit_consumed'
    gate.silence()
    assert not (await gate.claim())[0]
    assert gate.last_claim_reason == 'explicit_stop'
