"""WebRTC denoising before Gemini's speech detector, without amplitude gating.

Preserve timing and quiet speech: no frames are dropped, no VAD model download,
no extra endpointing delay. Gemini's lower-sensitivity VAD detects actual turns.
This is noise suppression, not speaker separation or echo cancellation.
"""
import logging

from livekit import rtc

log = logging.getLogger(__name__)


class SpeechInputFilter:
    def __init__(self):
        self.failed = False
        try:
            self.processor = rtc.AudioProcessingModule(
                noise_suppression=True, high_pass_filter=True,
                echo_cancellation=False, auto_gain_control=False)
        except Exception:
            self.failed = True
            log.warning('voice_noise_suppression_unavailable')

    def process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if self.failed:
            return frame
        # APM requires exactly 10 ms. The room supplies 20 ms / 16 kHz mono.
        samples = frame.sample_rate // 100
        if (frame.sample_rate not in (8000, 16000, 32000, 48000)
                or frame.num_channels != 1 or frame.samples_per_channel % samples):
            return frame
        raw = bytes(frame.data)
        output = bytearray()
        try:
            for offset in range(0, len(raw), samples * 2):
                chunk = rtc.AudioFrame(raw[offset:offset + samples * 2], frame.sample_rate, 1, samples)
                self.processor.process_stream(chunk)
                output.extend(bytes(chunk.data))
            return rtc.AudioFrame(bytes(output), frame.sample_rate, 1, frame.samples_per_channel)
        except Exception:
            # Audio availability wins over a failed optional denoiser.
            self.failed = True
            log.warning('voice_noise_suppression_unavailable')
            return frame
