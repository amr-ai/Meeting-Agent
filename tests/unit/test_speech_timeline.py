"""The speech timeline must reject clicks, coughs and steady tones, and time speech ends."""
import math
from array import array

from livekit import rtc

from app.voice.speech import MIN_RMS, SpeechTimeline, measure

FRAME_SAMPLES = 320  # 20 ms at 16 kHz


def frame(samples) -> rtc.AudioFrame:
    return rtc.AudioFrame(array('h', samples).tobytes(), 16000, 1, FRAME_SAMPLES)


def silence():
    return frame([0] * FRAME_SAMPLES)


def tone(amplitude, index=0, hz=250):
    return frame([int(amplitude * math.sin(2 * math.pi * hz * (i + index * FRAME_SAMPLES) / 16000))
                  for i in range(FRAME_SAMPLES)])


def speech(count=30):
    """Voiced, in-band, with a syllabic envelope; quiet enough to test the floor."""
    frames = []
    for index in range(count):
        amplitude = 300 + 700 * abs(math.sin(index * 0.8))
        frames.append(tone(amplitude, index=index))
    return frames


def cough(count=20):
    """Broadband burst: alternating full-scale samples put the zero-crossing rate at 1.0."""
    return [frame([20000 if i % 2 else -20000 for i in range(FRAME_SAMPLES)]) for _ in range(count)]


def click():
    return frame([30000] + [0] * (FRAME_SAMPLES - 1))


def run(timeline, frames):
    events = []
    for item in frames:
        outcome = timeline.push(item)
        if outcome:
            events.append(outcome)
    return events


def test_measure_reports_shape_not_only_energy():
    loud_noise = cough(1)[0]
    rms, zcr, crest = measure(loud_noise)
    assert rms > MIN_RMS
    assert zcr > 0.9, 'alternating samples must read as high zero-crossing rate'
    voiced_rms, voiced_zcr, voiced_crest = measure(speech(1)[0])
    assert voiced_zcr < 0.1, 'a 250 Hz tone is a voiced-rate signal'
    assert voiced_crest < 3


def test_silence_never_starts_speech():
    assert run(SpeechTimeline(), [silence()] * 40) == []


def test_single_click_never_starts_speech():
    assert run(SpeechTimeline(), [click()] + [silence()] * 40) == []


def test_cough_never_starts_speech():
    assert run(SpeechTimeline(), cough(20) + [silence()] * 40) == []


def test_steady_tone_never_starts_speech():
    # No syllabic modulation: a fan, hum or musical drone is not a request.
    assert run(SpeechTimeline(), [tone(1200, index=i) for i in range(40)]) == []


def test_speech_starts_after_confirmation_and_ends_after_hangover():
    started, ended = [], []
    timeline = SpeechTimeline(on_start=started.append,
                              on_end=lambda at, duration: ended.append((at, duration)))
    events = run(timeline, speech(20) + [silence()] * 40)

    assert events == ['start', 'end']
    assert len(started) == 1 and len(ended) == 1
    ended_at, duration_ms = ended[0]
    assert duration_ms > 0
    # End is stamped at the last voiced frame, not after the 400 ms hangover.
    assert ended_at <= started[0] + duration_ms / 1000 + 1e-6


def test_short_blip_inside_speech_does_not_end_the_turn():
    timeline = SpeechTimeline()
    events = run(timeline, speech(20))
    # 200 ms of quiet is less than the 400 ms hangover.
    events += run(timeline, [silence()] * 10 + speech(5))
    assert events == ['start'], 'a pause mid-sentence must not end the turn'
    events += run(timeline, [silence()] * 30)
    assert events == ['start', 'end']
