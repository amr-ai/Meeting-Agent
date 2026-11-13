"""Offline chat-load/frame-path measurements. Not Gemini/Meet end-to-end latency."""
import asyncio
import json
import math
import statistics
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
from livekit import rtc

from app.agent.gating import ResponseGate
from app.agent.worker import MeetingAgent
from app.config.settings import ROOT, Settings
from app.meeting.chat.service import ChatService
from app.meeting.events import EventProcessor
from app.storage.sqlite import Store


def summary(values):
    values = sorted(values)
    return {'samples': len(values), 'median_ms': statistics.median(values),
            'p95_ms': values[math.ceil(.95 * len(values)) - 1], 'max_ms': max(values)} if values else {}


class MockRecall:
    async def send_chat_message(self, bot, message):
        await asyncio.sleep(.02)
        return {'id': bot}


class MockText:
    async def generate(self, message, context, sources):
        await asyncio.sleep(.03)
        return 'A synthetic text-only reply.'


async def audio_frames(count=100):
    start = time.perf_counter()
    handoff, jitter, yielded = [], [], {}
    agent = SimpleNamespace(gate=ResponseGate('Khaled', local=True), published_audio=False,
                            telemetry=SimpleNamespace(emit=lambda *args, **kwargs: None))
    pcm = bytes([3, 1]) * 480

    async def source():
        for index in range(count):
            target = start + index * .02
            await asyncio.sleep(max(0, target - time.perf_counter()))
            now = time.perf_counter()
            jitter.append(max(0, now - target) * 1000)
            yielded['at'] = now
            yield rtc.AudioFrame(pcm, 24000, 1, 480)

    async for frame in MeetingAgent.realtime_audio_output_node(agent, source(), None):
        handoff.append((time.perf_counter() - yielded['at']) * 1000)
        assert bytes(frame.data) == pcm
    assert len(handoff) == count
    return {'output_node_handoff': summary(handoff), 'scheduled_frame_jitter': summary(jitter)}


async def main():
    settings = Settings().model_copy(update={'database_path': ROOT / ('data/chat-load-check-' + uuid4().hex + '.sqlite3'),
        'chat_send_interval_seconds': 0, 'chat_participant_per_minute': 10})
    db = Store(settings.database_path)
    await db.open()
    sid = str(uuid4())
    await db.create(sid, 'meeting', 3600)
    await db.update(sid, 'in_call_recording', 'synthetic-bot')
    processor = EventProcessor(db)
    async with httpx.AsyncClient() as http:
        service = ChatService(db, MockRecall(), http, settings, MockText())
        before = await audio_frames()
        processor.start()
        await service.start()

        async def feed():
            for index in range(100):
                now = time.time()
                text = f'Khaled, synthetic request {index}' if index < 6 else f'Unrelated synthetic discussion {index}'
                await processor.ingest(f'synthetic-{sid}-{index}', sid,
                    {'event': 'participant_events.chat_message', 'data': {'bot': {'id': 'synthetic-bot'}, 'data': {
                        'participant': {'id': index % 2, 'name': f'Tester {index % 2}'},
                        'timestamp': {'absolute': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'relative': index},
                        'data': {'text': text, 'to': 'everyone'}}}})

        try:
            _, after = await asyncio.gather(feed(), audio_frames())
            for _ in range(100):
                remaining = await db.rows("SELECT COUNT(*) AS n FROM events WHERE meeting_id=? AND processed=0", (sid,))
                if not remaining[0]['n']:
                    break
                await asyncio.sleep(.05)
            await asyncio.sleep(1)
            metrics = await db.rows('SELECT stage,duration_ms FROM chat_metrics WHERE meeting_id=?', (sid,))
            stages = {stage: summary([r['duration_ms'] for r in metrics if r['stage'] == stage])
                      for stage in sorted({r['stage'] for r in metrics})}
            stored = await db.rows('SELECT COUNT(*) AS n FROM meeting_chat_messages WHERE meeting_id=? AND source=?', (sid, 'recall-live'))
            result = {'stored_chat_messages': stored[0]['n'], 'scope': 'Actual application output-node handoff and event-loop scheduling under durable chat load; excludes Gemini, network, microphones and playback',
                'providers': 'Mock async text generation (30ms) and Recall send (20ms); no real messages sent',
                'synthetic_events': 100, 'chat_disabled': before, 'chat_enabled': after, 'chat_stages': stages}
            path = ROOT / 'docs/CHAT_LOAD_MEASUREMENTS.json'
            path.write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2))
        finally:
            await processor.close()
            await service.close()
            await db.close()


if __name__ == '__main__':
    asyncio.run(main())
