"""One decision per generation, before the first frame, with no waiting.

The previous gate awaited a transcript inside the audio generator. The provider only
emits the final transcript when generation ends, so the wait could not succeed; it
produced 5 s stalls and 26 dropped answers out of 193 generated responses.
"""
import time
from types import SimpleNamespace

from livekit import rtc

from app.agent.gating import ResponseGate
from app.agent.worker import MeetingAgent


class _Session:
    def __init__(self):
        self.interrupts = []

    def interrupt(self, force=False):
        self.interrupts.append(force)


class _Agent:
    realtime_audio_output_node = MeetingAgent.realtime_audio_output_node
    _cancel_generation = MeetingAgent._cancel_generation

    def __init__(self, gate, session=None):
        self.gate = gate
        self.published_audio = False
        self.events = []
        self.telemetry = SimpleNamespace(emit=lambda stage, **kw: self.events.append((stage, kw)))
        self._session = session or _Session()

    @property
    def session(self):
        return self._session

    def stages(self):
        return [stage for stage, _ in self.events]


def _frames(count=2):
    async def source():
        for _ in range(count):
            yield rtc.AudioFrame.create(24000, 1, 480)
    return source()


async def test_generation_before_any_transcript_is_cancelled_not_buffered():
    gate = ResponseGate('Khaled', local=False)
    session = _Session()
    agent = _Agent(gate, session)

    published = [f async for f in agent.realtime_audio_output_node(_frames(), None)]

    assert published == [], 'nothing is spoken without evidence that the caller was addressed'
    assert session.interrupts == [True], 'the generation is cancelled, not left running'
    assert agent.stages().count('generation_cancelled') == 1
    decision = [kw for stage, kw in agent.events if stage == 'response_gate_decided'][0]
    assert decision == {'authorized': False, 'reason': 'undetermined',
                        'epoch': decision['epoch'], 'gate_wait_ms': 0}
    # A refused generation must not consume or corrupt the permit of a later turn.
    assert gate.available is False and gate.claimed is False and gate.turn_committed is False


async def test_unaddressed_speech_is_denied_and_does_not_burn_the_turn():
    gate = ResponseGate('Khaled', local=False)
    session = _Session()
    agent = _Agent(gate, session)
    gate.transcript('We should review this design tomorrow')

    assert [f async for f in agent.realtime_audio_output_node(_frames(), None)] == []
    decision = [kw for stage, kw in agent.events if stage == 'response_gate_decided'][0]
    assert decision['reason'] == 'not_addressed'
    assert session.interrupts == [True]

    # The caller now addresses the agent: the same permit is still available.
    gate.transcript('Khaled, explain the design')
    assert len([f async for f in agent.realtime_audio_output_node(_frames(), None)]) == 2
    assert session.interrupts == [True], 'an authorized generation is never cancelled'


async def test_first_frame_is_published_without_waiting_for_a_decision():
    gate = ResponseGate('Khaled', local=False)
    agent = _Agent(gate)
    gate.transcript('Khaled, explain the design')

    published = [f async for f in agent.realtime_audio_output_node(_frames(count=3), None)]

    assert len(published) == 3
    decision = [kw for stage, kw in agent.events if stage == 'response_gate_decided'][0]
    assert decision['authorized'] is True and decision['gate_wait_ms'] == 0
    assert agent.stages().count('first_audio_handed_to_livekit') == 1
    assert 'response_gate_wait_started' not in agent.stages()
    assert agent.published_audio is True


async def test_stop_mid_stream_revokes_the_remainder():
    gate = ResponseGate('Khaled', local=False)
    agent = _Agent(gate)
    gate.transcript('Khaled, explain the design')
    frames = [rtc.AudioFrame.create(24000, 1, 480) for _ in range(3)]

    async def source():
        yield frames[0]
        gate.silence()
        yield frames[1]
        yield frames[2]

    assert [f async for f in agent.realtime_audio_output_node(source(), None)] == frames[:1]
    assert agent.published_audio is True


async def test_an_undecided_gate_never_consumes_the_decision_timeout():
    """The old path blocked for decision_timeout (5 s) on exactly this input."""
    gate = ResponseGate('Khaled', local=False, decision_timeout=5.0)
    agent = _Agent(gate)

    started = time.perf_counter()
    assert [f async for f in agent.realtime_audio_output_node(_frames(count=200), None)] == []
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f'the audio path waited {elapsed:.2f}s; it must not wait at all'
