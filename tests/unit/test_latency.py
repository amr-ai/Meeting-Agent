from scripts.latency_report import aggregate


def test_latency_does_not_subtract_different_clock_domains():
    rows = [
        {'session_id': 's', 'turn_id': 't', 'clock_id': 'worker',
         'stage': 'speech_end_observed', 'monotonic': 100},
        {'session_id': 's', 'turn_id': 't', 'clock_id': 'browser',
         'stage': 'first_audio_handed_to_livekit', 'monotonic': 101},
    ]
    assert aggregate(rows)['measured_turns'] == 0
    rows.append({'session_id': 's', 'turn_id': 't', 'clock_id': 'worker',
                 'stage': 'first_audio_handed_to_livekit', 'monotonic': 101.25})
    report = aggregate(rows)
    assert report['measured_turns'] == 1 and report['p95_ms'] == 1250
