from types import SimpleNamespace

import pytest
from livekit import rtc

from app.agent.gating import ResponseGate
from app.agent.worker import MeetingAgent


@pytest.mark.parametrize('local', [True, False])
async def test_shared_output_preserves_pcm_and_revokes_stopped_audio(local):
    gate = ResponseGate('Khaled', local=local, followup_seconds=30)
    gate.transcript('Khaled, answer this question')
    events = []
    agent = SimpleNamespace(gate=gate, published_audio=False,
        telemetry=SimpleNamespace(emit=lambda *args, **kwargs: events.append(args[0])))
    frames = [rtc.AudioFrame(bytes([3, 1]) * 480, 24000, 1, 480) for _ in range(3)]

    async def source():
        yield frames[0]
        gate.silence()
        yield frames[1]
        yield frames[2]

    output = [f async for f in MeetingAgent.realtime_audio_output_node(agent, source(), None)]
    assert len(output) == 1
    assert output[0] is frames[0]
    assert bytes(output[0].data) == bytes(frames[0].data)
    assert events.count('first_audio_handed_to_livekit') == 1


async def test_persona_switch_never_resumes_old_audio():
    gate = ResponseGate('Khaled', local=True)
    gate.transcript('bring Mariam')
    agent = SimpleNamespace(gate=gate, published_audio=False, persona_generation=0,
        persona_switching=False, telemetry=SimpleNamespace(emit=lambda *args, **kwargs: None))
    frames = [rtc.AudioFrame.create(24000, 1, 480) for _ in range(3)]
    async def source():
        yield frames[0]
        agent.persona_switching = True
        agent.persona_generation += 1
        yield frames[1]
        agent.persona_switching = False
        yield frames[2]
    output = [f async for f in MeetingAgent.realtime_audio_output_node(agent, source(), None)]
    assert output == frames[:1]
