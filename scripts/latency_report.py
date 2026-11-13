"""Aggregate observed stages without mixing monotonic clocks across processes."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], p: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def aggregate(rows: list[dict]) -> dict:
    turns: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        turns[(row['session_id'], row['turn_id'], row['clock_id'])].append(row)
    measurements = []
    errors = 0
    for (sid, tid, clock), events in turns.items():
        stages = {}
        for event in events:
            stages.setdefault(event['stage'], event['monotonic'])
        error = any(e['stage'] == 'error' or e.get('error') for e in events)
        errors += bool(error)
        latency = None
        if 'speech_end_observed' in stages and 'first_audio_handed_to_livekit' in stages:
            delta = stages['first_audio_handed_to_livekit'] - stages['speech_end_observed']
            latency = round(delta * 1000, 2) if delta >= 0 else None
        measurements.append({'session_id': sid, 'turn_id': tid, 'clock_id': clock,
            'speech_end_to_sdk_audio_ms': latency, 'error': bool(error), 'stages': stages})
    values = [m['speech_end_to_sdk_audio_ms'] for m in measurements
              if m['speech_end_to_sdk_audio_ms'] is not None]
    return {'measured_turns': len(values), 'observed_turns': len(turns),
        'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95),
        'max_ms': max(values) if values else None,
        'error_rate': errors / len(turns) if turns else None,
        'scope': 'Worker speech-end observation to SDK audio handoff; excludes browser and meeting playback',
        'turns': measurements}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, default=Path('data/latency-report.json'))
    args = parser.parse_args()
    rows = []
    for path in args.files:
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    result = aggregate(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'turns'}, indent=2))


if __name__ == '__main__':
    main()
