"""Measure real LiveKit/Gemini audio latency using disclosed synthetic speech."""
import argparse
import asyncio
import json
import math
import os
import signal
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path
from uuid import uuid4

from google import genai
from google.genai import types
from livekit import rtc

from app.config.settings import ROOT, Settings
from app.integrations.livekit import LiveKitService

QUESTIONS = {
    'arabic': 'صباح الخير، ممكن تقول اسمك بسرعة؟',
    'english': 'What is two plus two? Answer very briefly.',
    'mixed': 'يعني إيه API؟ جاوب في جملة واحدة.',
}


def audible(pcm: bytes) -> bool:
    samples = array('h', pcm)
    return bool(samples) and sum(abs(s) for s in samples) / len(samples) > 80


async def make_fixtures(settings: Settings, directory: Path, addressed: bool = False) -> dict[str, bytes]:
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    result = {}
    client = genai.Client(api_key=settings.google_api_key.get_secret_value(),
        http_options=types.HttpOptions(api_version=settings.gemini_api_version))
    try:
        for language, question in QUESTIONS.items():
            if addressed:
                question = ('يا خالد، ' if language != 'english' else 'Khaled, ') + question
            path = directory / f'{language}.wav'
            if path.exists():
                with wave.open(str(path), 'rb') as file:
                    result[language] = file.readframes(file.getnframes())
                continue
            async with asyncio.timeout(40):
                async with client.aio.live.connect(model=settings.gemini_model,
                        config=types.LiveConnectConfig(response_modalities=['AUDIO'])) as session:
                    await session.send_client_content(turns=types.Content(role='user', parts=[
                        types.Part(text='Read this question aloud exactly once. Do not answer it: ' + question)
                    ]), turn_complete=True)
                    chunks = [message.data async for message in session.receive() if message.data]
            pcm = b''.join(chunks)
            size = 960  # 20 milliseconds, 24 kHz, mono signed 16-bit PCM.
            voiced = [i for i in range(0, len(pcm), size) if audible(pcm[i:i + size])]
            if not voiced:
                raise RuntimeError('Fixture contained no audible speech')
            pcm = pcm[max(0, voiced[0] - size):voiced[-1] + size]
            with wave.open(str(path), 'wb') as file:
                file.setnchannels(1)
                file.setsampwidth(2)
                file.setframerate(24000)
                file.writeframes(pcm)
            result[language] = pcm
            print(f'Generated synthetic {language} speech: {len(pcm) / 48000:.2f}s', flush=True)
    finally:
        await client.aio.aclose()
        client.close()
    return result


async def run(turns: int, output: Path, mode: str = 'local', addressed: bool = False,
              fixture_directory: Path | None = None) -> None:
    settings = Settings()
    fixtures = await make_fixtures(settings, fixture_directory or ROOT / ('data/benchmark-addressed-inputs' if addressed else 'data/benchmark-inputs'), addressed)
    sid = str(uuid4())
    name = 'latency-probe-' + uuid4().hex[:8]
    service = LiveKitService(settings.model_copy(update={'agent_name': name}))
    room = rtc.Room()
    source = rtc.AudioSource(24000, 1, queue_size_ms=40)
    ready = asyncio.Event()
    received = asyncio.Event()
    readers: set[asyncio.Task] = set()
    input_frames: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(maxsize=10)
    pump_task = None
    state = {'last_audio': 0.0, 'first_audio': None, 'active': False}
    log_path = output.with_suffix('.worker.log')
    worker = None
    rows = []

    async def pump_microphone():
        next_frame = time.monotonic()
        while True:
            try:
                chunk, last = input_frames.get_nowait()
                queued = True
            except asyncio.QueueEmpty:
                chunk, last, queued = bytes(960), False, False
            await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
            if last:
                state['speech_end'] = time.monotonic() + source.queued_duration
            if queued:
                input_frames.task_done()
            next_frame = max(next_frame + .02, time.monotonic())
            await asyncio.sleep(max(0, next_frame - time.monotonic()))

    async def read(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                if audible(bytes(event.frame.data)):
                    now = time.monotonic()
                    state['last_audio'] = now
                    if state['active'] and state['first_audio'] is None:
                        state['first_audio'] = now
                        received.set()
        finally:
            await stream.aclose()

    @room.on('track_subscribed')
    def subscribed(track, publication, participant):
        if participant.identity == f'agent-{sid}' and publication.source == rtc.TrackSource.SOURCE_MICROPHONE:
            task = asyncio.create_task(read(track))
            readers.add(task)
            task.add_done_callback(readers.discard)

    @room.on('participant_attributes_changed')
    def attributes(changed, participant):
        if participant.identity == f'agent-{sid}' and participant.attributes.get('lk.agent.state') == 'listening':
            ready.set()

    @room.on('participant_connected')
    def participant_connected(participant):
        if participant.identity == f'agent-{sid}' and participant.attributes.get('lk.agent.state') == 'listening':
            ready.set()

    env = os.environ.copy()
    env['AGENT_NAME'] = name
    env['AGENT_HTTP_PORT'] = '0'  # Isolated probe must not collide with a running user worker.
    env['GEMINI_API_VERSION'] = settings.gemini_api_version
    try:
        with log_path.open('w', encoding='utf-8') as logfile:
            worker = await asyncio.to_thread(subprocess.Popen, [sys.executable, '-m', 'app.agent.worker', 'start'],
                cwd=ROOT, env=env, stdout=logfile, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            await room.connect(settings.livekit_url, service.token(sid))
            track = rtc.LocalAudioTrack.create_audio_track('benchmark-input', source)
            await room.local_participant.publish_track(track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            pump_task = asyncio.create_task(pump_microphone())
            await service.ensure({'id': sid, 'mode': mode})
            await asyncio.wait_for(ready.wait(), 90)
            await asyncio.sleep(8)
            for index in range(turns):
                language = list(fixtures)[index % len(fixtures)]
                pcm = fixtures[language]
                state['first_audio'] = None
                received.clear()
                state['active'] = True
                for position in range(0, len(pcm), 960):
                    chunk = pcm[position:position + 960]
                    chunk = chunk.ljust(960, b'\0')
                    await input_frames.put((chunk, position + 960 >= len(pcm)))
                await input_frames.join()
                speech_end = state['speech_end']
                try:
                    await asyncio.wait_for(received.wait(), timeout=25)
                except TimeoutError:
                    pass
                first = state['first_audio']
                delta = (first - speech_end) * 1000 if first is not None else None
                row = {'turn': index + 1, 'language': language,
                    'input_duration_seconds': len(pcm) / 48000,
                    'speech_end_to_first_received_audio_ms': round(delta, 1) if delta is not None and delta >= 0 else None,
                    'error': 'timeout' if first is None else ('early_response' if delta < 0 else None)}
                rows.append(row)
                print(json.dumps(row), flush=True)
                state['active'] = False
                if first is None:
                    raise RuntimeError('No audio response; stop benchmark and inspect worker log before retrying')
                # Wait for the preceding answer to finish before sending a new question.
                quiet_deadline = time.monotonic() + 20
                while time.monotonic() < quiet_deadline:
                    if time.monotonic() - state['last_audio'] > 1.5:
                        break
                    await asyncio.sleep(.1)
    finally:
        if pump_task:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        await room.disconnect()
        await source.aclose()
        for task in list(readers):
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await service.cleanup(sid)
        await service.close()
        if worker and worker.poll() is None:
            worker.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
            try:
                await asyncio.to_thread(worker.wait, timeout=20)
            except subprocess.TimeoutExpired:
                worker.terminate()
                await asyncio.to_thread(worker.wait, timeout=5)
        values = sorted(r['speech_end_to_first_received_audio_ms'] for r in rows
                        if r['speech_end_to_first_received_audio_ms'] is not None)
        result = {'scope': 'Real LiveKit RTC audio input -> Gemini Live -> received non-silent RTC audio; excludes browser speaker and Recall',
            'input': 'Gemini-generated synthetic speech, replayed at real-time speed',
            'mode': mode, 'addressed_fixtures': addressed,
            'model': settings.gemini_model, 'api_version': settings.gemini_api_version,
            'utc_finished': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'valid_turns': len(values), 'attempted_turns': len(rows),
            'p50_ms': values[math.ceil(len(values) * .5) - 1] if values else None,
            'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None,
            'max_ms': max(values) if values else None,
            'error_rate': sum(r['error'] is not None for r in rows) / len(rows) if rows else None,
            'turns': rows}
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_text, json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k != 'turns'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns', type=int, default=20, choices=range(1, 101))
    parser.add_argument('--output', type=Path, default=ROOT / 'data/live-latency-report.json')
    parser.add_argument('--mode', choices=['local', 'meeting'], default='local')
    parser.add_argument('--addressed', action='store_true', help='Use identical named-address fixtures for mode comparisons')
    args = parser.parse_args()
    asyncio.run(run(args.turns, args.output, args.mode, args.addressed))
