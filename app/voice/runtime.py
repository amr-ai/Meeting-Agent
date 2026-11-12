"""Startup mitigation for LiveKit's native soxr FFT-cache initialization race."""
import os
import threading

from livekit import rtc

_lock = threading.Lock()
_initialized = False


def initialize_audio_runtime() -> None:
    """Initialize native resampling serially before Windows job threads use it.

    This uses public RTC APIs, not a patched DLL or disabled native assertions.
    See https://github.com/livekit/rust-sdks/issues/1614.
    """
    global _initialized
    if os.name != 'nt':
        return
    with _lock:
        if _initialized:
            return
        # Prime both FFT precisions/quality recipes and the relevant voice rates.
        conversions = ((48000, 16000), (24000, 16000), (16000, 24000),
                       (24000, 48000), (44100, 24000), (22050, 24000))
        for input_rate, output_rate in conversions:
            frame = rtc.AudioFrame.create(input_rate, 1, input_rate // 10)
            for quality in rtc.AudioResamplerQuality:
                converter = rtc.AudioResampler(input_rate, output_rate,
                                               num_channels=1, quality=quality)
                converter.push(frame)
                converter.flush()
        _initialized = True


def setup_worker(_process: object) -> None:
    initialize_audio_runtime()
    from app.voice.network import warm_network_runtime
    warm_network_runtime()
