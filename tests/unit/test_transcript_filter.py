"""Short and valid utterances must survive; only exact non-speech phrases are ignored."""
from types import SimpleNamespace

import pytest

from app.agent.gating import ResponseGate
from app.agent.turns import VoiceTurns


class _Telemetry:
    def __init__(self):
        self.events = []

    def emit(self, stage, **kw):
        self.events.append((stage, kw))

    def begin_turn(self):
        self.events.append(('transcribed_turn_started', {}))


def _turns(local=True):
    gate = ResponseGate('Khaled,Khalid,خالد,يا خالد', local=local, followup_seconds=30)
    agent = SimpleNamespace(gate=gate, telemetry=_Telemetry(), pending_tools=set(),
                            meeting_link=None)
    return VoiceTurns(agent), gate


def _event(text, final=True, item_id='i1'):
    return SimpleNamespace(transcript=text, is_final=final, item_id=item_id)


@pytest.mark.parametrize('text', ['خالد', 'اسكت', 'أيوه', 'لا', 'تمام', 'Khaled', 'stop talking'])
def test_short_valid_utterances_are_never_filtered(text):
    turns, _ = _turns()
    assert turns.transcribed(_event(text)) is True


@pytest.mark.parametrize('text', ['thank you for watching', 'شكرا للمشاهدة', 'Thanks for watching.'])
def test_non_speech_artifacts_are_ignored_on_the_final(text):
    turns, _ = _turns()
    assert turns.transcribed(_event(text)) is False
    assert ('transcript_artifact_ignored', {}) in turns.agent.telemetry.events


@pytest.mark.parametrize('text', ['شكرا', 'thank you', 'شكرا يا خالد'])
def test_bare_acknowledgements_still_reach_the_gate(text):
    # These are real replies, and a wake name inside them is decisive.
    turns, _ = _turns()
    assert turns.transcribed(_event(text)) is True


def test_a_wake_name_is_exempt_even_inside_an_artifact_phrase():
    turns, gate = _turns(local=False)
    assert gate.has_wake_name('شكرا يا خالد')
    assert turns.transcribed(_event('شكرا يا خالد')) is True


def test_partials_are_never_filtered():
    turns, _ = _turns()
    assert turns.transcribed(_event('thank you for watching', final=False)) is True


def test_arabic_and_mixed_sentences_pass():
    turns, _ = _turns()
    for text in ['يا خالد، ابحث عن الموضوع ده وابعت النتيجة في الشات',
                 'What did we decide earlier?', 'بص على الشاشة اللي مشاركها وقولي إيه الموجود']:
        turns, _ = _turns()
        assert turns.transcribed(_event(text)) is True
