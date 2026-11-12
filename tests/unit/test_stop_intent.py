import time
from types import SimpleNamespace

import pytest

from app.agent.gating import ResponseGate
from app.agent.worker import MeetingAgent
from tests.unit.test_voice_tool_regression import agent_state


@pytest.mark.parametrize('text', [
    'خالد خلاص ابحثلي عن ورقة بحثية واكتبها في الشات',
    'خلاص دورلي على آخر بحث', 'شكرا', 'خالد شكرا',
    'خالد متسكتش وكمل البحث', 'خالد لما اقول اسكت تبقى تسكت',
    'عايزك تكتب كلمة اسكت في الشات', 'خالد اشرح معنى خلاص',
    "Khaled, don't stop talking", 'Please search for stop speaking detection',
    'Khaled say be quiet in Arabic',
])
@pytest.mark.parametrize('speaking', [False, True])
def test_work_requests_thanks_negation_and_quoted_stop_never_mute(text, speaking):
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.transcript('Khaled help')
    assert not gate.is_stop_request(text, speaking=speaking)
    assert not gate.silenced and gate.tools_allowed()


@pytest.mark.parametrize('text', [
    'يا خالد اسكت بقى', 'خالد ممكن تسكت لو سمحت', 'خالد خلاص كفاية',
    'خالد خليك سامع', 'خالد اسكت وخليك سامع', 'Khaled stop talking please',
    'Khaled please be quiet now',
])
def test_explicit_spoken_stop_still_works(text):
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.transcript('Khaled help')
    assert gate.is_stop_request(text)


def test_cumulative_partial_done_word_cannot_stop_a_later_work_request():
    gate = ResponseGate('خالد', followup_seconds=30)
    gate.transcript('خالد')
    assert not gate.is_stop_request('خالد خلاص', final=False, speaking=True)
    assert not gate.is_stop_request('خالد خلاص ابحثلي عن RAG', final=True, speaking=True)
    assert gate.is_stop_request('خالد خلاص', final=True)
    assert not gate.is_stop_request('خالد اسكت', final=False, speaking=True)


async def test_pause_and_stale_unchanged_recall_metadata_preserve_followup_window():
    gate = ResponseGate('خالد', followup_seconds=30)
    gate.set_reported_speaker('amr')
    gate.transcript('خالد')
    await gate.claim()
    gate.speaker_observed = time.monotonic() - 20
    gate.reset()
    assert not gate.silenced
    assert gate.transcript('سامعني؟')
    assert (await gate.claim())[0]
    gate.set_reported_speaker('different-person')
    gate.reset()
    assert not gate.transcript('This is unrelated conversation')


async def test_model_cannot_silence_without_current_explicit_spoken_request():
    agent, events = agent_state()
    agent.is_speaking = True
    agent.gate.transcript('Khaled help')
    agent.turns.transcribed(SimpleNamespace(item_id='request', transcript='خالد ابحث عن RAG', is_final=True))
    agent.gate.transcript('خالد ابحث عن RAG')
    agent.stop_speech = lambda: pytest.fail('Model tool must not mute on a task request')
    result = await MeetingAgent.stop_speaking(agent, stop_requested=False)
    assert not result['stop_applied'] and not agent.gate.silenced
    assert 'unrequested_stop_tool_rejected' in events
    assert (await agent.gate.claim())[0]


async def test_model_cannot_reuse_old_stop_intent_after_new_epoch():
    agent, _ = agent_state()
    agent.is_speaking = False
    agent.gate.transcript('Khaled help')
    agent.turns.transcribed(SimpleNamespace(item_id='stop', transcript='خالد اسكت', is_final=True))
    agent.gate.reset()
    agent.gate.transcript('Khaled new request')
    agent.stop_speech = lambda: pytest.fail('Stale stop transcript cannot mute a new turn')
    assert not (await MeetingAgent.stop_speaking(agent, stop_requested=False))['stop_applied']


async def test_model_stop_tool_accepts_current_explicit_stop_once():
    agent, _ = agent_state()
    agent.is_speaking = True
    agent.turns.transcribed(SimpleNamespace(item_id='stop', transcript='خالد اسكت', is_final=True))
    agent.gate.transcript('خالد اسكت')
    applied = []

    def stop():
        applied.append(True)
        agent.gate.silence()

    agent.stop_speech = stop
    assert (await MeetingAgent.stop_speaking(agent, stop_requested=False))['state'] == 'listening_only'
    await MeetingAgent.stop_speaking(agent, stop_requested=False)
    assert applied == [True] and agent.gate.silenced


@pytest.mark.parametrize('text', ['خلاص يا خالد', 'اسكت يا خالد', 'يا خالد اسكت', 'يا خالد خلاص',
                                  'خلاص ياخالد', '  يا  خالد ، اسكت  ', 'Khaled please stop talking'])
def test_meeting_stop_requires_a_complete_direct_named_command(text):
    gate = ResponseGate('Khaled,خالد')
    assert gate.is_stop_request(text, final=True)
    assert not gate.is_stop_request(text, final=False)


@pytest.mark.parametrize('text', ['خلاص', 'بس', 'تمام', 'كفاية', 'خلاص كده نكمل الشرح',
    'بس عندي سؤال', 'تمام خلاص', 'خلينا نخلص الموضوع', 'محمد قال خلاص يا خالد',
    'ما تقولش اسكت يا خالد', 'خلاص يا خالد، كمل شرحك', 'لو قلت اسكت يا خالد هتعمل ايه',
    '"اسكت يا خالد"', 'Khaled do not stop talking'])
def test_meeting_filler_quotes_and_continuations_never_stop(text):
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.transcript('Khaled help')
    assert not gate.is_stop_request(text, speaking=True)


async def test_stop_voice_interrupts_output_without_cancelling_pending_work():
    import asyncio
    agent, _ = agent_state()
    agent.telemetry.turn = 'turn'
    agent.meeting_link = None
    interrupted = []
    agent.session = SimpleNamespace(interrupt=lambda **kwargs: interrupted.append(kwargs))
    work = asyncio.create_task(asyncio.Event().wait())
    agent.pending_tools.add(work)
    MeetingAgent.stop_speech(agent)
    await asyncio.sleep(0)
    assert agent.gate.silenced and interrupted == [{'force': True}]
    assert not work.cancelled() and not work.done()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize('continuation', [False, True])
async def test_pending_named_stop_never_prompts_ack_or_mutes_a_later_continuation(continuation):
    from livekit.agents.llm import ToolResult
    agent, _ = agent_state()
    agent.is_speaking = True
    event = SimpleNamespace(item_id='stop-candidate', transcript='يا خالد خلاص', is_final=False)
    agent.turns.transcribed(event)
    agent.gate.transcript(event.transcript, final=False)
    result = await MeetingAgent.stop_speaking(agent, stop_requested=False)
    assert isinstance(result, ToolResult) and not result.reply_required
    assert not agent.gate.silenced
    text = 'يا خالد خلاص كمل شرحك' if continuation else 'يا خالد خلاص'
    agent.turns.transcribed(SimpleNamespace(item_id='stop-candidate', transcript=text, is_final=True))
    agent.stop_speech = agent.gate.silence
    result = await MeetingAgent.stop_speaking(agent, stop_requested=False)
    assert agent.gate.silenced is not continuation
    assert result.get('stop_applied') is False if continuation else result['state'] == 'listening_only'


async def test_semantic_stop_does_not_require_a_keyword_or_repeated_name():
    agent, _ = agent_state()
    agent.gate = ResponseGate('Khaled,خالد', followup_until_stop=True)
    agent.gate.transcript('يا خالد')
    event = SimpleNamespace(item_id='natural-stop', transcript='خليني أكمل كلامي وانت اسمعني للآخر', is_final=True)
    agent.turns.transcribed(event)
    agent.gate.transcript(event.transcript)
    agent.is_speaking = True
    agent.stop_speech = agent.gate.silence
    assert (await MeetingAgent.stop_speaking(agent, stop_requested=True))['state'] == 'listening_only'
    assert agent.gate.silenced and not agent.gate.conversation_active


@pytest.mark.parametrize('continuation', [False, True])
async def test_semantic_partial_stop_waits_for_same_completed_utterance(continuation):
    from livekit.agents.llm import ToolResult
    agent, _ = agent_state()
    agent.gate = ResponseGate('Khaled,خالد', followup_until_stop=True)
    agent.gate.transcript('يا خالد')
    text = 'اسمعني للآخر'
    partial = SimpleNamespace(item_id='stop', transcript=text, is_final=False)
    agent.turns.transcribed(partial)
    agent.gate.transcript(text, final=False)
    result = await MeetingAgent.stop_speaking(agent, stop_requested=True)
    assert isinstance(result, ToolResult) and not result.reply_required
    assert not agent.gate.silenced
    final = SimpleNamespace(item_id='stop', transcript=text + (' لا كمل عادي' if continuation else ''), is_final=True)
    agent.turns.transcribed(final)
    assert agent.turns.consume_semantic_stop(final) is (not continuation)
    assert not agent.turns.consume_semantic_stop(final)  # Never reapply a consumed decision.


async def test_old_semantic_stop_cannot_cross_a_new_user_turn():
    agent, _ = agent_state()
    agent.gate = ResponseGate('Khaled', followup_until_stop=True)
    agent.gate.transcript('Khaled')
    partial = SimpleNamespace(item_id='old', transcript='let me finish speaking', is_final=False)
    agent.turns.transcribed(partial)
    agent.gate.transcript(partial.transcript, final=False)
    await MeetingAgent.stop_speaking(agent, stop_requested=True)
    final = SimpleNamespace(item_id='new', transcript=partial.transcript, is_final=True)
    agent.turns.transcribed(final)
    assert not agent.turns.consume_semantic_stop(final)
