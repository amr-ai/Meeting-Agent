import time

import pytest

from app.agent.gating import ResponseGate


@pytest.mark.parametrize('text', ['Khaled, explain the API', 'يا خالد ممكن تشرح LangGraph؟',
                                  'خَالِد، tell us more', 'KHALID search this please'])
async def test_addressed_turn_gets_one_audio_permit(text):
    gate = ResponseGate('Khaled,Khalid,خالد')
    assert gate.transcript(text)
    allowed, epoch = await gate.claim()
    assert allowed and epoch == gate.epoch
    assert not (await gate.claim())[0]


async def test_human_discussion_and_new_turn_revoke_authorization():
    gate = ResponseGate('Khaled,خالد', decision_timeout=.05)
    assert not gate.transcript('We should deploy the API tomorrow')
    assert not gate.tools_allowed()
    assert not (await gate.claim())[0]
    assert not gate.transcript('Khaledness is not a wake name')
    gate.transcript('Khaled search the web')
    gate.reset()
    assert not gate.tools_allowed()
    assert not (await gate.claim())[0]


async def test_local_mode_does_not_require_wake_name():
    gate = ResponseGate('Khaled', local=True)
    gate.reset()
    assert (await gate.claim())[0]


async def test_partial_wake_name_releases_audio_before_delayed_final():
    import asyncio
    gate = ResponseGate('Khaled,خالد')
    claim = asyncio.create_task(gate.claim())
    await asyncio.sleep(.01)
    assert not gate.transcript('يا', final=False)
    assert not claim.done()
    assert gate.transcript('يا خالد ممكن ترد', final=False)
    assert (await asyncio.wait_for(claim, timeout=.2))[0]
    # A final arriving after generation must not rearm a consumed response permit.
    gate.transcript('يا خالد ممكن ترد', final=True)
    assert not (await gate.claim())[0]


async def test_late_final_authorization_is_not_discarded_at_old_350ms_cutoff():
    import asyncio
    gate = ResponseGate('Khaled', decision_timeout=1)
    claim = asyncio.create_task(gate.claim())
    await asyncio.sleep(.4)
    gate.transcript('Khaled, are you there?')
    assert (await claim)[0]


async def test_partial_without_wake_name_cannot_publish_audio():
    gate = ResponseGate('Khaled', decision_timeout=.02)
    gate.transcript('please review the design', final=False)
    assert not (await gate.claim())[0]
    gate.transcript('please review the design', final=True)
    assert not gate.tools_allowed()


@pytest.mark.parametrize('phrase', ['خلاص يا خالد', 'اسكت يا خالد', 'خالد كفاية', 'بلاش تكمل يا خالد', 'خالد خليك سامع',
                                   'خالد بس كده', 'Khaled stop talking', "Khaled that's enough"])
def test_stop_phrases_revoke_current_output(phrase):
    gate = ResponseGate('Khaled,خالد')
    gate.transcript('Khaled help us')
    assert gate.is_stop_request(phrase, speaking=True)
    epoch = gate.epoch
    gate.silence()
    assert gate.epoch != epoch
    assert not gate.tools_allowed()


async def test_meeting_stop_requires_fresh_name_and_does_not_resume_old_response():
    gate = ResponseGate('Khaled,خالد', decision_timeout=.02)
    gate.transcript('Khaled help us')
    assert (await gate.claim())[0]
    gate.silence()
    stopped_epoch = gate.epoch
    assert not (await gate.claim())[0]
    gate.reset()
    gate.transcript('What were we saying?')
    assert not gate.tools_allowed()
    assert not (await gate.claim())[0]
    gate.transcript('يا خالد سؤال جديد', final=False)
    allowed, epoch = await gate.claim()
    assert allowed and epoch > stopped_epoch


def test_ordinary_human_discussion_does_not_become_a_stop_command():
    gate = ResponseGate('Khaled,خالد')
    assert not gate.is_stop_request('المشروع خلاص خلص وهنبدأ في المشروع الجديد', speaking=False)


async def test_wake_opens_same_speakers_directed_followup_window():
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.set_reported_speaker('amr')
    gate.transcript('خالد')
    assert (await gate.claim())[0]
    gate.reset()
    assert gate.transcript('تعرف مين موجود في الاجتماع؟')
    assert (await gate.claim())[0]
    assert not (await gate.claim())[0]


async def test_different_reported_speaker_needs_a_fresh_wake_name():
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.set_reported_speaker('amr')
    gate.transcript('Khaled')
    await gate.claim()
    gate.set_reported_speaker('alice')
    gate.reset()
    assert not gate.transcript('Who is in the meeting?')
    assert not (await gate.claim())[0]
    assert gate.transcript('Khaled, who is here?')
    assert (await gate.claim())[0]


async def test_stop_and_expiry_close_directed_followups():
    gate = ResponseGate('Khaled,خالد', followup_seconds=30)
    gate.set_reported_speaker('amr')
    gate.transcript('Khaled')
    gate.silence()
    gate.reset()
    assert not gate.transcript('Tell me who is here')
    assert not (await gate.claim())[0]

    gate.transcript('Khaled')
    gate.active_until = time.monotonic() - 1
    gate.reset()
    assert not gate.transcript('Tell me who is here')
    assert not (await gate.claim())[0]


def test_followup_authorization_revokes_if_reported_speaker_changes():
    gate = ResponseGate('Khaled', followup_seconds=30)
    gate.set_reported_speaker('amr')
    gate.transcript('Khaled')
    gate.reset()
    assert gate.transcript('Who is here?', final=False)
    gate.set_reported_speaker('alice')
    assert not gate.tools_allowed()
    assert not gate.transcript('Who is here?', final=False)


async def test_local_stop_cancels_old_audio_but_next_turn_needs_no_wake():
    gate = ResponseGate('Khaled', local=True)
    old_epoch = gate.epoch
    gate.silence()
    assert not (await gate.claim())[0]
    gate.reset()
    assert gate.transcript('What is two plus two?')
    allowed, epoch = await gate.claim()
    assert allowed and epoch > old_epoch
    assert gate.tools_allowed()


@pytest.mark.parametrize('quiet_seconds', [65, 600, 3600])
async def test_persistent_conversation_survives_silence_until_explicit_stop(monkeypatch, quiet_seconds):
    clock = [100.0]
    monkeypatch.setattr('app.agent.gating.time.monotonic', lambda: clock[0])
    gate = ResponseGate('Khaled,خالد', followup_seconds=30, followup_until_stop=True)
    gate.set_reported_speaker('amr')
    assert not gate.transcript('Unaddressed discussion before wake')
    gate.transcript('يا خالد')
    assert (await gate.claim())[0]
    clock[0] += quiet_seconds
    gate.reset()
    assert not gate.silenced and gate.conversation_active
    assert gate.transcript('نكمل موضوع الشاشة؟')
    assert gate.tools_allowed() and (await gate.claim())[0]
    gate.silence()
    clock[0] += quiet_seconds
    gate.reset()
    assert not gate.transcript('نكمل؟')
    assert not gate.tools_allowed() and not gate.conversation_active
    assert gate.transcript('يا خالد نكمل')
    assert (await gate.claim())[0]


async def test_persistent_followup_does_not_authorize_a_different_reported_speaker():
    gate = ResponseGate('Khaled', followup_until_stop=True)
    gate.set_reported_speaker('amr')
    gate.transcript('Khaled')
    await gate.claim()
    gate.set_reported_speaker('other')
    gate.reset()
    assert not gate.transcript('Unrelated discussion')
    gate.set_reported_speaker('amr')
    gate.reset()
    assert gate.transcript('Continue where we left off')
    assert (await gate.claim())[0]
