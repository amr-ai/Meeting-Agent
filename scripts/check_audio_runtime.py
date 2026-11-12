"""Offline, fresh-process stress check of the native-audio startup mitigation."""
import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from livekit import rtc

from app.voice.runtime import initialize_audio_runtime


def convert(index: int) -> int:
    rates = ((24000, 16000), (48000, 16000), (16000, 24000), (24000, 48000))
    input_rate, output_rate = rates[index % len(rates)]
    converter = rtc.AudioResampler(input_rate, output_rate, num_channels=1)
    frame = rtc.AudioFrame.create(input_rate, 1, input_rate // 10)
    frames = converter.push(frame) + converter.flush()
    return sum(f.samples_per_channel for f in frames)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--processes', type=int, default=10, choices=range(1, 101))
    args = parser.parse_args()
    if args.child:
        initialize_audio_runtime()
        with ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(convert, range(64)))
        if not all(sizes):
            raise RuntimeError('Resampling produced empty output')
        print('64 concurrent resampling operations passed', flush=True)
        return
    for index in range(args.processes):
        subprocess.run([sys.executable, __file__, '--child'], check=True, timeout=30)
        print(f'Fresh process {index + 1}/{args.processes} passed', flush=True)


if __name__ == '__main__':
    main()
