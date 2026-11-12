"""Isolate Gemini native audio from LiveKit RTC using an existing synthetic WAV."""
import asyncio
import json
import time
import wave

from livekit import rtc

from app.agent.worker import build_instructions, create_model
from app.config.settings import ROOT, Settings
from app.voice.network import warm_network_runtime
from app.voice.runtime import initialize_audio_runtime


async def main():
    await asyncio.to_thread(warm_network_runtime)
    initialize_audio_runtime()
    connected, heard = asyncio.Event(), asyncio.Event()
    readers = set()
    result = {'scope': 'Actual Gemini native audio through LiveKit Google plugin; no RTC, Meet, chat or response gate',
              'input': 'Existing synthetic English WAV', 'transcribed': False, 'audio_received': False,
              'errors': [], 'speech_end_to_first_audio_ms': None}
    settings = Settings()
    session = create_model(settings).session()
    speech_end = first = None

    @session.on('metrics_collected')
    def metrics(event):
        if getattr(event, 'acquire_time', 0):
            connected.set()

    @session.on('input_audio_transcription_completed')
    def transcribed(event):
        result['transcribed'] = True

    @session.on('error')
    def error(event):
        result['errors'].append({'type': type(event.error).__name__,
                                 'recoverable': event.recoverable})

    async def read(event):
        nonlocal first
        async for message in event.message_stream:
            async for frame in message.audio_stream:
                if any(frame.data):
                    if first is None:
                        first = time.monotonic()
                    result['audio_received'] = True
                    heard.set()

    @session.on('generation_created')
    def generation(event):
        task = asyncio.create_task(read(event))
        readers.add(task)
        task.add_done_callback(readers.discard)

    try:
        await session.update_instructions(build_instructions(settings, 'local'))
        await session.update_tools([])
        await asyncio.wait_for(connected.wait(), timeout=30)
        with wave.open(str(ROOT / 'data/voice-research-inputs/english.wav'), 'rb') as wav:
            rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        frame_bytes = rate // 50 * 2
        for start in range(0, len(pcm), frame_bytes):
            chunk = pcm[start:start + frame_bytes].ljust(frame_bytes, b'\0')
            session.push_audio(rtc.AudioFrame(chunk, rate, 1, frame_bytes // 2))
            await asyncio.sleep(.02)
        speech_end = time.monotonic()
        # Keep the microphone stream alive with silence, like the production bridge.
        for _ in range(1250):
            if heard.is_set():
                break
            session.push_audio(rtc.AudioFrame(bytes(frame_bytes), rate, 1, frame_bytes // 2))
            await asyncio.sleep(.02)
        if first and speech_end:
            result['speech_end_to_first_audio_ms'] = round((first - speech_end) * 1000, 1)
    finally:
        await session.aclose()
        await asyncio.gather(*readers, return_exceptions=True)
        result['utc_finished'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (ROOT / 'docs/VOICE_TRANSPORT_PROBE.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
    if not result['audio_received']:
        raise RuntimeError('No native audio received; see scoped probe output')


if __name__ == '__main__':
    asyncio.run(main())
