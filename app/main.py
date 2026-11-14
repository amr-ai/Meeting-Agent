import asyncio
import hmac
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config.settings import ROOT, Settings
from app.integrations.livekit import LiveKitService
from app.integrations.recall import RecallClient
from app.meeting.chat.service import ChatService
from app.meeting.events import EventProcessor, parse_utterance
from app.meeting.security import (
    sign_bridge,
    validate_meeting_url,
    verify_bridge,
    verify_video,
    verify_webhook,
)
from app.memory.archive import SpeechArchive
from app.memory.context import context
from app.observability.telemetry import configure_logging
from app.screen.receiver import Screens
from app.screen.vision import Vision
from app.screen.workflow import ScreenWorkflow
from app.storage.chat_repository import ChatRepository
from app.storage.sqlite import Store
from app.workflows.chat_tools import ChatToolsWorkflow

log = logging.getLogger(__name__)

SUPERVISOR_INTERVAL_SECONDS = 15


class ChatToolRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    operation: str = Field(pattern='^(read_meeting_chat|search_meeting_chat|get_chat_context|send_meeting_chat|research_and_send_meeting_chat)$')
    query: str = Field(default='', max_length=500)
    participant: str = Field(default='', max_length=200)
    since: float | None = Field(default=None, ge=0, le=1e16)
    until: float | None = Field(default=None, ge=0, le=1e16)
    limit: int = Field(default=10, ge=1, le=50)
    message: str = Field(default='', max_length=1500)
    idempotency_key: str = Field(default='', max_length=180)


class FrameReference(BaseModel):
    model_config = {'extra': 'forbid'}
    stream_id: str = Field(max_length=128)
    generation: str = Field(max_length=64)
    revision: int = Field(ge=0)


class VisionRequest(BaseModel):
    model_config = {'extra': 'forbid', 'allow_inf_nan': False}
    question: str = Field(min_length=1, max_length=1000)
    presenter: str = Field(default='', max_length=200)
    previous: bool = False
    compare: bool = False
    crop: tuple[float, float, float, float] | None = None
    expected: FrameReference | None = None
    research_to_chat: bool = False
    idempotency_key: str = Field(default='', max_length=180)


class JoinRequest(BaseModel):
    meeting_url: str
    consent_confirmed: bool


class BridgeRequest(BaseModel):
    token: str = Field(max_length=4096)


class ContextRequest(BaseModel):
    query: str = Field(default='', max_length=500)


class ConversationRequest(BaseModel):
    event_key: str = Field(max_length=200)
    role: str = Field(pattern='^(user|assistant|control)$')
    text: str = Field(min_length=1, max_length=20000)
    interrupted: bool = False
    speaker: str | None = Field(default=None, pattern='^(Khaled|Mariam|Youssef|Adam)$')
    observed_at: float | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)


class BridgeLatencyRequest(BridgeRequest):
    turn_id: str = Field(max_length=100)
    duration_ms: float = Field(ge=0, le=120000)
    speech_end_ms: float = Field(ge=0, le=1e16)
    first_received_audio_ms: float = Field(ge=0, le=1e16)
    clock_origin_ms: float = Field(ge=0, le=1e16)


def create_app(settings: Settings | None = None, livekit=None, recall=None, chat_responder=None,
               vision_provider=None) -> FastAPI:
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        app.state.store = Store(s.database_path)
        await app.state.store.open()
        app.state.screens = Screens(app.state.store, s)
        await app.state.screens.start()
        app.state.speech_archive = SpeechArchive(app.state.store)
        await app.state.speech_archive.start()
        async with httpx.AsyncClient(follow_redirects=False) as http:
            app.state.vision = Vision(app.state.screens, http, s, vision_provider)
            await app.state.vision.start()
            if s.screen_enabled:
                app.state.store.screen_context = lambda sid: {
                    **app.state.screens.context(sid), 'visual_tasks': app.state.vision.context(sid)}
            app.state.lk = livekit or LiveKitService(s)
            app.state.recall = recall or RecallClient(http, s)
            app.state.events = EventProcessor(app.state.store, s.event_queue_size, s.assistant_name)
            app.state.events.start()
            app.state.chat = ChatService(app.state.store, app.state.recall, http, s, chat_responder)
            await app.state.chat.start()
            app.state.monitor_errors = 0
            app.state.empty_finals = set()
            app.state.monitor_stopped = False
            # Built once: compiling these per request cost 15-72 ms of event-loop CPU
            # on every voice chat-tool and screen call before any work started.
            app.state.chat_tools = ChatToolsWorkflow(app.state.store, app.state.chat.sender)
            app.state.screen_workflow = ScreenWorkflow(app.state.screens, app.state.vision, s)
            monitor = asyncio.create_task(supervise(app, s), name='meeting-supervisor')
            app.state.monitor = monitor
            monitor.add_done_callback(lambda task: _restart_supervisor(app, s, task))
            try:
                yield
            finally:
                app.state.monitor_stopped = True
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                await app.state.events.close()
                await app.state.chat.close()
                await app.state.vision.close()
                await app.state.screens.close()
                await app.state.speech_archive.close()
                await app.state.lk.close()
                await app.state.store.close()

    app = FastAPI(title='Meeting Voice Agent', lifespan=lifespan)
    app.state.settings = s

    async def authorized(authorization: str = Header(default='')) -> None:
        if not hmac.compare_digest(authorization, 'Bearer ' + s.admin_api_key.get_secret_value()):
            raise HTTPException(401, 'Invalid API credentials')

    async def meeting(sid: str) -> dict:
        result = await app.state.store.get(sid)
        if not result:
            raise HTTPException(404, 'Meeting not found')
        return result

    @app.middleware('http')
    async def secure_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
                                 'X-Content-Type-Options': 'nosniff'})
        return response

    @app.get('/health')
    async def health():
        failed = await app.state.store.rows('SELECT COUNT(*) AS count FROM events WHERE attempts>=5 AND processed=0')
        task = app.state.events.task
        healthy = (task is not None and not task.done() and not app.state.monitor.done()
                   and all(not task.done() for task in app.state.chat.tasks))
        return {'status': 'ok' if healthy and not failed[0]['count'] else 'degraded',
                'protocol_version': 12 if s.screen_enabled else 10,
                'screen_features': {'capture': s.screen_enabled, 'vision': s.screen_enabled and s.vision_enabled,
                                    'passive': s.screen_enabled and s.vision_enabled and s.vision_passive_enabled},
                'dead_letter_events': failed[0]['count'], 'monitor_errors': app.state.monitor_errors}

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/bridge')
    async def bridge():
        return FileResponse(ROOT / 'static/bridge/index.html')

    @app.post('/api/local', dependencies=[Depends(authorized)])
    async def local():
        sid = str(uuid4())
        m = await app.state.store.create(sid, 'local', s.session_max_seconds)
        try:
            await app.state.lk.ensure(m)
            await app.state.store.update(sid, 'active')
        except Exception:
            await app.state.store.update(sid, 'failed')
            raise HTTPException(502, 'LiveKit session could not be created') from None
        return {'session_id': sid, 'bridge_token': sign_bridge(sid, s)}

    async def local_browser(request: Request) -> None:
        # The public Recall tunnel must never become a keyless admin proxy.
        # Require loopback peer, loopback Host and an explicit same-origin UI request.
        if (not request.client or request.client.host not in {'127.0.0.1', '::1'} or
                request.url.hostname not in {'localhost', '127.0.0.1', '::1'} or
                request.headers.get('origin') != str(request.base_url).rstrip('/') or
                request.headers.get('x-voice-ui') != '1' or
                any(h in request.headers for h in ('forwarded', 'x-forwarded-for', 'x-forwarded-host'))):
            raise HTTPException(403, 'Open the voice page on localhost on this computer')

    @app.post('/api/ui/session', dependencies=[Depends(local_browser)])
    async def ui_session():
        # Settings already holds .env credentials. Return only this session's capability.
        return await local()

    @app.post('/api/ui/leave', dependencies=[Depends(local_browser)])
    async def ui_leave(body: BridgeRequest):
        try:
            sid = verify_bridge(body.token, s)
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, 'Invalid or expired voice session') from None
        m = await meeting(sid)
        if m['mode'] != 'local':
            raise HTTPException(403, 'This endpoint is for local voice sessions')
        return await leave(sid)

    @app.post('/api/meetings', dependencies=[Depends(authorized)])
    async def join(body: JoinRequest):
        if not body.consent_confirmed:
            raise HTTPException(400, 'Confirm recording and transcription consent first')
        try:
            validate_meeting_url(body.meeting_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        sid = str(uuid4())
        m = await app.state.store.create(sid, 'meeting', s.session_max_seconds)
        try:
            await app.state.lk.ensure(m)
            # A fragment keeps the capability out of access logs and HTTP referrers.
            url = s.public_base_url.rstrip('/') + '/bridge#' + sign_bridge(sid, s)
            bot = await app.state.recall.create(body.meeting_url, url)
            await app.state.store.update(sid, 'joining', bot['id'])
        except Exception as exc:
            await app.state.store.update(sid, 'failed')
            try:
                await app.state.lk.cleanup(sid)
            except Exception:
                log.error('join_cleanup_failed', extra={'session_id': sid})
            log.error('join_failed', extra={'session_id': sid, 'error_type': type(exc).__name__})
            raise HTTPException(502, 'Meeting creation failed; inspect provider before retrying an ambiguous timeout') from None
        return {'session_id': sid, 'bot_id': bot['id']}

    @app.get('/api/meetings/{sid}', dependencies=[Depends(authorized)])
    async def status(sid: str):
        return await meeting(sid)

    @app.post('/api/meetings/{sid}/leave', dependencies=[Depends(authorized)])
    async def leave(sid: str):
        m = await meeting(sid)
        if m['bot_id'] and m['status'] not in {'left', 'done'}:
            await app.state.recall.leave(m['bot_id'])
        await app.state.store.update(sid, 'left')
        app.state.vision.cancel_session(sid)
        app.state.screens.invalidate(sid)
        await app.state.lk.cleanup(sid)
        return {'status': 'left'}

    @app.delete('/api/meetings/{sid}', dependencies=[Depends(authorized)])
    async def delete(sid: str):
        m = await meeting(sid)
        if m['status'] not in {'left', 'done', 'failed'}:
            raise HTTPException(409, 'Leave the meeting before deleting stored data')
        app.state.vision.cancel_session(sid, forget=True)
        app.state.screens.invalidate(sid)
        await app.state.store.cleanup_journals(sid)
        await app.state.store.execute('DELETE FROM meetings WHERE id=?', (sid,))
        return {'deleted': True}

    @app.get('/api/meetings/{sid}/transcript', dependencies=[Depends(authorized)])
    async def transcript(sid: str):
        await meeting(sid)
        return await app.state.store.transcript(sid)

    @app.post('/api/meetings/{sid}/context', dependencies=[Depends(authorized)])
    async def get_context(sid: str, body: ContextRequest):
        await meeting(sid)
        return {'context': await context(app.state.store, sid, body.query)}

    @app.get('/api/meetings/{sid}/live-context', dependencies=[Depends(authorized)])
    async def live_context(sid: str, after: int = 0):
        await meeting(sid)
        if after < 0:
            raise HTTPException(422, 'Cursor must be nonnegative')
        data = await app.state.store.live_context(sid, after)
        if s.screen_enabled:
            data['visual_context'] = app.state.store.screen_context(sid)
        return data

    @app.post('/api/meetings/{sid}/conversation', dependencies=[Depends(authorized)])
    async def conversation(sid: str, body: ConversationRequest):
        await meeting(sid)
        speaker = (body.speaker or s.assistant_name) if body.role == 'assistant' else 'Unknown'
        await app.state.store.add_conversation(sid, body.event_key, body.role, body.text,
            speaker=speaker, source='agent-playout' if body.role == 'assistant' else 'gemini-live',
            interrupted=body.interrupted, created=body.observed_at)
        return {'accepted': True}

    @app.get('/api/meetings/{sid}/chat', dependencies=[Depends(authorized)])
    async def chat_history(sid: str, query: str = '', participant: str = '', limit: int = 20):
        await meeting(sid)
        return {'messages': await ChatRepository(app.state.store).history(sid, query, participant, limit=limit)}

    @app.post('/api/meetings/{sid}/chat/tools', dependencies=[Depends(authorized)])
    async def chat_tool(sid: str, body: ChatToolRequest):
        await meeting(sid)
        return await app.state.chat_tools.run(body.operation, body.model_dump(exclude={'operation'}, exclude_none=True),
                                              authorized=True, sid=sid)

    @app.get('/api/meetings/{sid}/chat/jobs', dependencies=[Depends(authorized)])
    async def chat_jobs(sid: str):
        await meeting(sid)
        return {'jobs': await app.state.store.rows('SELECT id,query,status,error,created FROM chat_voice_jobs WHERE meeting_id=? ORDER BY created DESC LIMIT 20', (sid,))}

    @app.post('/api/meetings/{sid}/chat/jobs/{jid}/cancel', dependencies=[Depends(authorized)])
    async def cancel_chat_job(sid: str, jid: str):
        await meeting(sid)
        try:
            return await app.state.chat.cancel_research(sid, jid)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get('/api/meetings/{sid}/chat/metrics', dependencies=[Depends(authorized)])
    async def chat_metrics(sid: str):
        await meeting(sid)
        return {'scope': 'Chat stages only; provider timestamps may use a different clock',
                'metrics': await app.state.chat.metrics(sid)}

    @app.get('/api/meetings/{sid}/latency', dependencies=[Depends(authorized)])
    async def latency(sid: str):
        await meeting(sid)
        rows = await app.state.store.rows('SELECT turn_id,duration_ms,payload,created FROM bridge_metrics WHERE meeting_id=? ORDER BY created', (sid,))
        values = sorted(row['duration_ms'] for row in rows)
        return {'scope': 'Browser audio energy: detected input speech end to received agent audio; excludes acoustic Meet playback',
                'samples': len(values),
                'p50_ms': values[math.ceil(len(values) * .5) - 1] if values else None,
                'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None,
                'max_ms': max(values) if values else None, 'turns': rows}

    @app.post('/api/bridge/session')
    async def bridge_session(body: BridgeRequest):
        try:
            sid = verify_bridge(body.token, s)
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, 'Invalid or expired bridge session') from None
        m = await meeting(sid)
        if m['status'] in {'left', 'done', 'failed'} or time.time() > m['expires']:
            raise HTTPException(410, 'Session has ended')
        await app.state.lk.ensure(m)
        return {'server_url': s.livekit_url, 'participant_token': app.state.lk.token(sid),
                'agent_identity': f'agent-{sid}', 'session_id': sid,
                'bridge_token': sign_bridge(sid, s, min(int(m['expires']),
                    int(time.time()) + s.bridge_ttl_seconds)),
                'renew_after_seconds': max(30, s.bridge_ttl_seconds // 2)}

    @app.post('/api/bridge/latency')
    async def bridge_latency(body: BridgeLatencyRequest):
        try:
            sid = verify_bridge(body.token, s)
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, 'Invalid bridge session') from None
        m = await meeting(sid)
        if m['status'] in {'left', 'done', 'failed'} or time.time() > m['expires']:
            raise HTTPException(410, 'Session has ended')
        observed = body.first_received_audio_ms - body.speech_end_ms
        if abs(observed - body.duration_ms) > 2:
            raise HTTPException(422, 'Inconsistent browser-clock measurement')
        payload = body.model_dump(exclude={'token'})
        await app.state.store.execute('INSERT OR IGNORE INTO bridge_metrics VALUES (?,?,?,?,?)',
            (sid, body.turn_id, body.duration_ms, json.dumps(payload), time.time()))
        await app.state.store.add_conversation(sid, 'bridge-latency-' + body.turn_id, 'control',
            f'Bridge latency: {body.duration_ms:.0f} ms (detected speech end to received agent audio; excludes Meet playback).',
            source='bridge-measurement')
        return {'accepted': True}

    @app.post('/webhooks/recall/')
    async def webhook(request: Request):
        if int(request.headers.get('content-length', '0')) > 1_000_000:
            raise HTTPException(413, 'Payload too large')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1_000_000:
                raise HTTPException(413, 'Payload too large')
        try:
            eid = verify_webhook(bytes(raw), dict(request.headers), s)
        except (ValueError, TypeError):
            raise HTTPException(401, 'Invalid webhook authentication') from None
        try:
            payload = json.loads(raw)
            bot_id = payload.get('data', {}).get('bot', {}).get('id')
        except (ValueError, AttributeError):
            raise HTTPException(422, 'Invalid event payload') from None
        matches = await app.state.store.rows('SELECT id FROM meetings WHERE bot_id=?', (bot_id,))
        if not matches:
            # Retry if creation and webhook delivery raced.
            raise HTTPException(503, 'Bot is not registered yet')
        await app.state.events.ingest(eid, matches[0]['id'], payload)
        return {'accepted': True}

    @app.post('/api/meetings/{sid}/vision', dependencies=[Depends(authorized)])
    async def analyze_screen(sid: str, body: VisionRequest):
        await meeting(sid)
        return await app.state.screen_workflow.run(sid, body.model_dump(exclude_none=True))

    @app.get('/api/meetings/{sid}/screens', dependencies=[Depends(authorized)])
    async def screens(sid: str):
        await meeting(sid)
        return app.state.screens.context(sid)

    @app.post('/api/meetings/{sid}/screen-frame', dependencies=[Depends(authorized)])
    async def capture_screen(sid: str, body: VisionRequest):
        await meeting(sid)
        return await app.state.screen_workflow.capture(sid, body.model_dump(exclude_none=True))

    @app.websocket('/ws/recall-video/{sid}/')
    async def video_socket(websocket: WebSocket, sid: str):
        if not s.screen_enabled:
            await websocket.close(code=1008)
            return
        try:
            verify_video(websocket.query_params.get('token', ''), sid, s)
            bound = await app.state.store.get(sid)
            if (not bound or bound['mode'] != 'meeting' or time.time() > bound['expires']
                    or bound['status'] in {'left', 'done', 'failed', 'fatal'}):
                raise ValueError('Inactive screen session')
            connection = app.state.screens.connect(sid)
        except (ValueError, jwt.PyJWTError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                text = await websocket.receive_text()
                if len(text.encode('utf-8')) > s.screen_max_message_bytes:
                    await websocket.close(code=1009)
                    break
                payload = json.loads(text)
                wrapper = payload.get('data') if isinstance(payload, dict) else None
                if not isinstance(wrapper, dict) or not isinstance(wrapper.get('data'), dict):
                    raise ValueError('Invalid video envelope')
                current = await app.state.store.get(sid)
                if (not current or current['status'] in {'left', 'done', 'failed', 'fatal'}
                        or time.time() > current['expires']):
                    await websocket.close(code=1008)
                    break
                if not current['bot_id']:
                    continue  # Provider create and early frames can race; no unbound data is used.
                if wrapper.get('bot', {}).get('id') != current['bot_id']:
                    raise ValueError('Screen bot does not match session')
                app.state.screens.ingest(sid, connection, payload)
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError, AttributeError):
            await websocket.close(code=1003)
        finally:
            app.state.screens.disconnect(sid, connection)

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


def _restart_supervisor(app: FastAPI, s: Settings, task: asyncio.Task) -> None:
    """Supervision must outlive its own loop; a dead monitor stops expiry and reconcile."""
    if task.cancelled() or app.state.monitor_stopped:
        return
    error = task.exception()
    log.error('meeting_supervisor_stopped',
              extra={'error_type': type(error).__name__ if error else 'none'})
    app.state.monitor = asyncio.create_task(supervise(app, s), name='meeting-supervisor-restarted')
    app.state.monitor.add_done_callback(lambda inner: _restart_supervisor(app, s, inner))


async def supervise(app: FastAPI, s: Settings) -> None:
    while True:
        try:
            await supervise_once(app, s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # store.rows() and purge() used to sit outside the per-meeting handler, so one
            # "database is locked" or disk-full killed expiry, leave and reconcile for the
            # whole process lifetime with no restart.
            app.state.monitor_errors += 1
            log.error('meeting_supervision_cycle_failed',
                      extra={'error_type': type(exc).__name__})
        await asyncio.sleep(SUPERVISOR_INTERVAL_SECONDS)


async def supervise_once(app: FastAPI, s: Settings) -> None:
    store, recall, lk = app.state.store, app.state.recall, app.state.lk
    meetings = await store.rows("SELECT * FROM meetings WHERE status != 'failed'")
    for m in meetings:
        sid = m['id']
        try:
            if m['status'] not in {'left', 'done'}:
                if time.time() > m['expires']:
                    if m['bot_id']:
                        await recall.leave(m['bot_id'])
                    await store.update(sid, 'left')
                    await lk.cleanup(sid)
                    continue
                if m['bot_id']:
                    bot = await recall.status(m['bot_id'])
                    changes = bot.get('status_changes', [])
                    status = changes[-1]['code'] if changes else m['status']
                    await store.update(sid, status)
                    if status in {'done', 'fatal'}:
                        await store.update(sid, 'done')
                        await lk.cleanup(sid)
                    else:
                        await lk.ensure(m)
                elif m['mode'] == 'local':
                    await lk.ensure(m)
            if m['bot_id']:
                marker = await store.rows("SELECT text FROM memory WHERE meeting_id=? AND kind='reconciled'", (sid,))
                if marker:
                    continue
                final = await recall.final_transcript(m['bot_id'])
                if final is not None and not final:
                    # An empty provider artifact is not an instruction to erase stored
                    # speech: reconcile(sid, []) deletes every utterance and writes the
                    # 'reconciled' marker that blocks all later inserts (sqlite.py:112,
                    # events.py:102-105). Keep the current data and retry only if the
                    # provider later returns actual content.
                    if sid not in app.state.empty_finals:
                        app.state.empty_finals.add(sid)
                        log.warning('final_transcript_empty', extra={'session_id': sid})
                    continue
                if final is not None:
                    items = [u for i, data in enumerate(final)
                             if (u := parse_utterance(sid, m['bot_id'], data, f'final-{i}',
                                                      source='recall-final'))]
                    if final and not items:
                        raise ValueError('Final transcript schema did not contain utterances')
                    await store.reconcile(sid, items)
                    app.state.empty_finals.discard(sid)
                    await store.execute('INSERT OR IGNORE INTO memory VALUES (?,?,?)',
                                         (sid, 'reconciled', 'true'))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.monitor_errors += 1
            log.error('meeting_supervision_failed', extra={'session_id': sid,
                                                         'error_type': type(exc).__name__})
    await store.purge(s.retention_days)
