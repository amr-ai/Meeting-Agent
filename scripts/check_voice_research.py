"""Credentialed synthetic voice -> queued research -> captured chat, without Meet."""
import asyncio
import json
import os
import socket
import time
from uuid import UUID, uuid4

import uvicorn

from app.config.settings import ROOT, Settings
from app.main import create_app
from scripts import benchmark_voice


class IsolatedLiveKit:
    async def ensure(self, meeting):
        pass

    async def cleanup(self, sid):
        pass

    async def close(self):
        pass


class CaptureRecall:
    def __init__(self):
        self.messages = []

    async def send_chat_message(self, bot_id, message):
        assert bot_id.startswith('synthetic-')
        self.messages.append({'bot_id': bot_id, 'message': message})
        return {'id': bot_id}

    async def status(self, bot_id):
        return {'status_changes': [{'code': 'in_call_recording'}]}

    async def final_transcript(self, bot_id):
        return None

    async def leave(self, bot_id):
        pass


async def main():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    os.environ['API_BASE_URL'] = f'http://127.0.0.1:{port}'
    settings = Settings().model_copy(update={'database_path': ROOT / f'data/voice-research-{uuid4().hex}.sqlite3'})
    captured = CaptureRecall()
    app = create_app(settings, IsolatedLiveKit(), captured)

    @app.middleware('http')
    async def register_test_room(request, call_next):
        pieces = request.url.path.split('/')
        if (len(pieces) > 3 and pieces[1:3] == ['api', 'meetings'] and
                request.headers.get('authorization') == 'Bearer ' + settings.admin_api_key.get_secret_value()):
            sid = str(UUID(pieces[3]))
            if not await app.state.store.get(sid):
                await app.state.store.create(sid, 'meeting', 3600)
                await app.state.store.update(sid, 'in_call_recording', 'synthetic-' + sid)
        return await call_next(request)

    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            if serving.done():
                await serving
                raise RuntimeError('Isolated API stopped')
            await asyncio.sleep(.1)
        if not server.started:
            raise RuntimeError('Isolated API did not start')
        benchmark_voice.QUESTIONS = {
            'arabic': 'يا خالد ابحث عن RAG واكتب تعريفه مع رابط مصدر في الشات.',
            'english': 'Khaled, can you hear me? Answer briefly.',
            'mixed': 'يا خالد يعني إيه API؟ جاوب في جملة واحدة.'}
        output = ROOT / 'data/voice-research-audio.json'
        await benchmark_voice.run(3, output, 'meeting', False,
                                  fixture_directory=ROOT / 'data/voice-research-inputs')
        for _ in range(90):
            jobs = await app.state.store.rows('SELECT status,error FROM chat_voice_jobs')
            if jobs and all(j['status'] not in {'pending', 'running', 'waiting'} for j in jobs):
                break
            await asyncio.sleep(.5)
        jobs = await app.state.store.rows('SELECT status,error FROM chat_voice_jobs')
        result = {'scope': 'Actual synthetic Gemini/LiveKit voice and actual public research/text model; Recall delivery captured locally, no Google Meet joined or chat sent',
                  'jobs': jobs, 'captured_messages': len(captured.messages),
                  'audio': json.loads(output.read_text(encoding='utf-8')) if output.exists() else None,
                  'utc_finished': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        (ROOT / 'docs/VOICE_RESEARCH_PROBE.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
    finally:
        server.should_exit = True
        await serving


if __name__ == '__main__':
    asyncio.run(main())
