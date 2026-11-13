from livekit import rtc

from app.agent.gating import ResponseGate
from app.agent.worker import MeetingAgent
from app.observability.telemetry import Telemetry


async def test_actual_agent_output_node_suppresses_unaddressed_audio(settings):
    import httpx
    gate = ResponseGate(settings.wake_names)
    telemetry = Telemetry('session', settings.latency_path)
    gate.transcript('We should review this design tomorrow')
    async with httpx.AsyncClient() as http:
        agent = MeetingAgent(settings, gate, telemetry, http, 'session', 'meeting')

        async def frames():
            yield rtc.AudioFrame.create(24000, 1, 480)

        assert [f async for f in agent.realtime_audio_output_node(frames(), {})] == []
        gate.transcript('Khaled, explain the design')
        assert len([f async for f in agent.realtime_audio_output_node(frames(), {})]) == 1
        assert [f async for f in agent.realtime_audio_output_node(frames(), {})] == []
        assert telemetry.queue.qsize() > 0
        # Merely processing native voice audio must not invoke the search graph.
        assert not agent.pending_tools
