from app.agent.worker import create_model


def test_livekit_google_model_explicitly_routes_to_live_beta_endpoint(settings):
    # google-genai 3.0 defaults to v1, which returned HTTP 404 for Live.
    # Verify the real plugin retains an explicit Live endpoint override.
    model = create_model(settings)
    assert model._opts.api_version == 'v1beta'
    assert model._opts.model == settings.gemini_model
    assert model._opts.input_audio_transcription.language_codes == ['ar', 'en']
    assert 'خالد' in model._opts.input_audio_transcription.custom_vocabulary


def test_modes_share_working_voice_runtime_without_overriding_interrupt_defaults(monkeypatch):
    from app.agent import worker
    calls = []
    monkeypatch.setattr(worker, 'AgentSession', lambda **kwargs: calls.append(kwargs))
    worker.create_session('native-model')
    assert calls == [{'llm': 'native-model', 'allow_interruptions': True}]


def test_local_instructions_match_confirmed_working_baseline(settings):
    from app.agent.worker import build_instructions
    local = build_instructions(settings, 'local')
    assert 'Respond naturally to each question' in local
    assert 'silent meeting metadata updates' not in local


async def test_disabled_screen_is_reported_honestly_without_visual_tools(settings):
    import httpx

    from app.agent.gating import ResponseGate
    from app.agent.worker import MeetingAgent
    from app.observability.telemetry import Telemetry
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as http:
        agent = MeetingAgent(settings, ResponseGate('Khaled'), Telemetry('meeting', settings.latency_path),
                             http, 'meeting', 'meeting')
        assert agent.screen_tools is None
        assert 'Screen understanding is not enabled for this call' in agent.instructions
        assert 'do not claim the image is blurry' in agent.instructions
