from app.agent.worker import audio_room_options
from app.voice import runtime


def test_gemini_room_input_matches_native_plugin_format():
    options = audio_room_options('test')
    assert options.audio_input.sample_rate == 16000
    assert options.audio_input.num_channels == 1
    assert options.audio_output.sample_rate == 24000


def test_windows_startup_initialization_is_serial_and_idempotent(monkeypatch):
    initialized = []

    class Converter:
        def __init__(self, *args, **kwargs):
            initialized.append(args)

        def push(self, frame):
            return []

        def flush(self):
            return []

    monkeypatch.setattr(runtime, '_initialized', False)
    monkeypatch.setattr(runtime.rtc, 'AudioResampler', Converter)
    runtime.initialize_audio_runtime()
    first_count = len(initialized)
    runtime.initialize_audio_runtime()
    if runtime.os.name == 'nt':
        assert first_count == 6 * len(runtime.rtc.AudioResamplerQuality)
        assert len(initialized) == first_count
