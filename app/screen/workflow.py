"""Read visual evidence, then enqueue existing durable research only if requested."""
import asyncio
import base64
import hashlib
import json
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from app.memory.context import context
from app.screen.decoder import crop_image
from app.storage.chat_repository import ChatRepository


class VisualState(TypedDict, total=False):
    sid: str
    arguments: dict
    result: dict
    error: str


class ScreenWorkflow:
    async def capture(self, sid, args):
        """Capture on request for native Live input; never call the image model."""
        if args.get('research_to_chat') or args.get('previous') or args.get('compare'):
            return {'error': 'Use the visual workflow for research or historical comparisons'}
        state = await self.validate({'sid': sid, 'arguments': args})
        if state.get('error'):
            return state
        try:
            await self.screens.refresh(sid, args.get('presenter', ''))
            frame = self.screens.select(sid, args.get('presenter', ''))
            expected = args.get('expected')
            if expected and any(frame[key] != expected[key] for key in ('stream_id', 'generation')):
                raise ValueError('The presenter/share changed since the question; repeat using the current share')
            data = frame['jpeg']
            if args.get('crop'):
                data = await asyncio.to_thread(crop_image, data, args['crop'],
                    self.settings.screen_max_pixels, self.settings.screen_max_edge)
            screen = self.screens.streams.get((sid, frame['stream_id']))
            if not screen or not screen.active or screen.generation != frame['generation']:
                raise ValueError('The shared screen disconnected or changed while capturing')
            source = {key: frame[key] for key in ('id', 'stream_id', 'generation', 'revision',
                'presenter', 'participant_id', 'relative_timestamp', 'width', 'height')}
            return {'image': base64.b64encode(data).decode(), 'mime_type': 'image/jpeg',
                    'source': source, 'crop': args.get('crop')}
        except ValueError as error:
            return {'error': str(error)}

    def __init__(self, screens, vision, settings):
        self.screens, self.vision, self.settings = screens, vision, settings
        self.store = screens.store
        self.repo = ChatRepository(self.store)
        graph = StateGraph(VisualState)
        graph.add_node('validate', self.validate)
        graph.add_node('analyze', self.analyze)
        graph.add_node('enqueue', self.enqueue)
        graph.add_edge(START, 'validate')
        graph.add_conditional_edges('validate', lambda s: END if s.get('error') or s.get('result') else 'analyze')
        graph.add_conditional_edges('analyze', lambda s: END if s.get('error') or not s['arguments'].get('research_to_chat') else 'enqueue')
        graph.add_edge('enqueue', END)
        self.graph = graph.compile()

    @staticmethod
    def spec(arguments):
        selected = {key: arguments.get(key) for key in ('question', 'presenter', 'previous', 'compare', 'crop')}
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()

    async def validate(self, state):
        if not self.settings.screen_enabled or not self.settings.vision_enabled:
            return {'error': 'Screen vision is disabled'}
        meeting = await self.store.get(state['sid'])
        if (not meeting or meeting['mode'] != 'meeting' or time.time() > meeting['expires']
                or meeting['status'] in {'left', 'done', 'failed', 'fatal'}):
            return {'error': 'No active meeting screen session'}
        args = state['arguments']
        if args.get('research_to_chat'):
            key = args.get('idempotency_key')
            if not isinstance(key, str) or not 1 <= len(key) <= 180:
                return {'error': 'A stable authorized request key is required before public posting'}
            existing = await self.store.rows('SELECT * FROM chat_voice_jobs WHERE meeting_id=? AND request_key=?',
                                             (state['sid'], key))
            if existing:
                try:
                    if json.loads(existing[0]['instructions']).get('spec_hash') != self.spec(args):
                        return {'error': 'Request key belongs to a different visual question'}
                except ValueError:
                    return {'error': 'Request key is already in use'}
                return {'result': {'job_id': existing[0]['id'], 'status': existing[0]['status'], 'accepted': False,
                                   'note': 'Existing durable task; no second analysis or message was created'}}
        return {}

    async def analyze(self, state):
        args, sid = state['arguments'], state['sid']
        previous = await context(self.store, sid, args['question'], limit=6000)
        result = await self.vision.analyze(sid, args['question'], presenter=args.get('presenter', ''),
            previous=args.get('previous', False), compare=args.get('compare', False), crop=args.get('crop'),
            expected=args.get('expected'), request_id=args.get('idempotency_key'), meeting_context=previous)
        return {'error': result['error']} if result.get('error') else {'result': result}

    async def enqueue(self, state):
        result, args, sid = state['result'], state['arguments'], state['sid']
        query = result.get('search_query', '').strip()
        if not result.get('readable') or not 2 <= len(query) <= 500:
            return {'error': 'No readable public error/topic was established; clarify or zoom. Nothing was posted'}
        if re.search(r'(?i)api.?key|password|secret|private|كلمة السر|مفتاح سري|AIza|ghp_', query):
            return {'error': 'Visible private/credential information cannot be used for public research'}
        for name in type(self.settings).model_fields:
            value = getattr(self.settings, name)
            if isinstance(value, SecretStr) and len(value.get_secret_value()) >= 8 and value.get_secret_value() in query:
                return {'error': 'A configured credential cannot be included in research'}
        source = result['source']
        instructions = json.dumps({'spec_hash': self.spec(args), 'request': args['question'][:400],
            'visual_evidence': result['answer'][:450],
            'source': {key: source[key] for key in ('generation', 'revision', 'participant_id', 'relative_timestamp')},
            'destination': 'public Meet chat', 'format': 'Concise verified solution with a source link'}, ensure_ascii=False)
        # This is the existing durable workflow, not a second job system. Captured
        # visual evidence belongs to the original request even if slides change later.
        queued = await self.repo.enqueue_research(sid, query, instructions, args['idempotency_key'])
        self.screens.emit(sid, 'visual_research_job_persisted', request_id=args['idempotency_key'], job_id=queued['job_id'])
        return {'result': {**queued, 'source': source}}

    async def run(self, sid, arguments):
        try:
            result = await self.graph.ainvoke({'sid': sid, 'arguments': arguments})
            return {'error': result['error']} if result.get('error') else result.get('result', {})
        except Exception as exc:
            self.screens.emit(sid, 'visual_workflow_failed', error_type=type(exc).__name__)
            return {'error': 'Visual operation failed; no delivery is confirmed'}
