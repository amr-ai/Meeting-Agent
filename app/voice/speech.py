"""Model-free speech boundary detection from the frames already in flight.

Gemini reports activity around generation lifecycle events and emits the final
transcript only when generation ends (realtime_api.py:1587-1603), so neither can
answer "when did the user stop speaking". That is the quantity the latency budget
is defined on, and today the app cannot measure it at all.

This derives the boundary locally from cheap per-frame arithmetic. It is a
confirmation signal, not a classifier: it never drops, delays or gates a frame, and
it is never the sole authority for starting or stopping a turn. Its limits are
explicit — a sustained musical drone that starts abruptly looks like speech onset.
That is acceptable because every consumer combines it with the provider's own
event, and it is what makes a cough, a click or a door stop interrupting the agent.
"""
import math
import time
from collections import deque

# Tuned for the 16 kHz mono 20 ms frames room_io supplies (worker.py:77-82).
# Named so they can move to Settings without hunting for magic numbers.
MIN_RMS = 180.0            # ~-45 dBFS on int16; quiet speech still passes
QUIET_RMS_RATIO = 0.6      # hangover floor, relative to MIN_RMS
VOICED_ZCR = (0.02, 0.30)  # voiced speech sits low; clicks and noise burst high
MAX_CREST = 10.0           # impulsive transients (keyboard, door, packet) exceed this
MIN_MODULATION = 0.12      # syllabic envelope; steady tones, hums and fans do not
DEFAULT_FRAME_MS = 20.0


def measure(frame) -> tuple[float, float, float]:
    """Return (rms, zero-crossing rate, crest factor) for one int16 mono frame."""
    data = frame.data
    samples = data if data.format == 'h' else data.cast('h')
    count = len(samples)
    if not count:
        return 0.0, 0.0, 0.0
    total = 0.0
    peak = 0
    crossings = 0
    previous = 0
    for value in samples:
        magnitude = value if value >= 0 else -value
        if magnitude > peak:
            peak = magnitude
        total += float(value) * value
        if (value >= 0) != (previous >= 0):
            crossings += 1
        previous = value
    rms = math.sqrt(total / count)
    crest = peak / rms if rms else math.inf
    return rms, crossings / count, crest


def is_voiced(rms: float, zcr: float, crest: float) -> bool:
    return rms >= MIN_RMS and VOICED_ZCR[0] <= zcr <= VOICED_ZCR[1] and crest <= MAX_CREST


class SpeechTimeline:
    """Debounced speech start/end with a hangover, from frame features only.

    ``confirm_ms`` of consistently speech-shaped audio is required before speech is
    declared, so a click cannot start a turn. ``hangover_ms`` of quiet is required
    before it ends, so a stop consonant or a breath cannot end one.
    """

    def __init__(self, *, on_start=None, on_end=None, confirm_ms: int = 300,
                 hangover_ms: int = 400, frame_ms: float = DEFAULT_FRAME_MS,
                 clock=time.monotonic):
        frame_ms = frame_ms if frame_ms > 0 else DEFAULT_FRAME_MS
        self._on_start, self._on_end = on_start, on_end
        self._frame_ms = frame_ms
        self._hangover_frames = max(1, round(hangover_ms / frame_ms))
        self._clock = clock
        self._window = deque(maxlen=max(1, round(confirm_ms / frame_ms)))
        self._quiet_run = 0
        self._started_at: float | None = None
        self._last_voiced_at: float | None = None

    @property
    def speaking(self) -> bool:
        return self._started_at is not None

    def _modulation(self) -> float:
        values = [rms for rms, _, _ in self._window]
        mean = sum(values) / len(values)
        if mean <= 0:
            return 0.0
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return math.sqrt(variance) / mean

    def push(self, frame) -> str | None:
        """Feed one frame; return 'start', 'end' or None."""
        now = self._clock()
        rms, zcr, crest = measure(frame)
        self._window.append((rms, zcr, crest))
        if self._started_at is None:
            if len(self._window) < self._window.maxlen:
                return None
            voiced_frames = sum(1 for entry in self._window if is_voiced(*entry))
            if voiced_frames < 0.6 * len(self._window) or self._modulation() < MIN_MODULATION:
                return None
            self._started_at = now
            self._last_voiced_at = now
            self._quiet_run = 0
            if self._on_start is not None:
                self._on_start(now)
            return 'start'
        if rms >= MIN_RMS * QUIET_RMS_RATIO:
            self._quiet_run = 0
            self._last_voiced_at = now
            return None
        self._quiet_run += 1
        if self._quiet_run < self._hangover_frames:
            return None
        started, ended = self._started_at, self._last_voiced_at or now
        self._started_at = None
        self._last_voiced_at = None
        if self._on_end is not None:
            self._on_end(ended, (ended - started) * 1000)
        return 'end'
