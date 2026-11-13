"""Bounded volatile screen state. All mutation stays on the API event loop."""
import asyncio
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from uuid import uuid4

from app.observability.telemetry import Telemetry
from app.screen.decoder import Decoder


@dataclass
class Screen:
    participant_id: str | None
    presenter: str
    generation: str = field(default_factory=lambda: uuid4().hex)
    active: bool = True
    stamp: float = -1
    relative: float = -1
    sampled: float = 0
    revision: int = 0
    current: dict | None = None
    previous: dict | None = None
    summary: dict | None = None


class Screens:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.connections = {}
        self.watermarks = {}
        self.floors = {}
        self.streams = {}
        self.telemetry = {}
        self.queue = asyncio.Queue(maxsize=settings.screen_queue_size)
        self.task = None
        self.executor = None
        self.decoders = {}
        self.dropped = 0
        self.on_change = None
        self.on_disconnect = None
        self.cleaned = 0.0

    async def start(self):
        if not self.settings.screen_enabled:
            return
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='screen-decode')
        self.task = asyncio.create_task(self.run(), name='screen-decoder')

    def emit(self, sid, stage, **data):
        if sid not in self.telemetry:
            tel = Telemetry(sid, self.settings.latency_path)
            tel.start()
            self.telemetry[sid] = tel
        self.telemetry[sid].emit(stage, **data)

    def connect(self, sid):
        if sid not in self.connections and len(self.connections) >= 8:
            raise ValueError('Screen connection capacity reached')
        self.invalidate(sid)
        if self.on_disconnect:
            self.on_disconnect(sid)
        token = uuid4().hex
        self.connections[sid] = token
        self.floors[sid] = self.watermarks.get(sid, -1)
        if len(self.watermarks) > 64:
            for old_sid in list(self.watermarks):
                if old_sid not in self.connections:
                    self.watermarks.pop(old_sid, None)
                    self.floors.pop(old_sid, None)
                    break
        return token

    def disconnect(self, sid, token):
        if self.connections.get(sid) == token:
            self.connections.pop(sid, None)
            self.invalidate(sid)
            if self.on_disconnect:
                self.on_disconnect(sid)

    def invalidate(self, sid):
        for key in [key for key in self.streams if key[0] == sid]:
            self.streams.pop(key)

    def ingest(self, sid, connection, payload):
        if self.connections.get(sid) != connection:
            return
        event = payload.get('event', '')
        data = payload.get('data', {}).get('data', {})
        if not isinstance(data, dict):
            raise ValueError('Invalid screen envelope')
        participant = data.get('participant') or {}
        pid = str(participant['id']) if participant.get('id') is not None else None
        stream_id = pid or str(payload.get('data', {}).get('video_separate', {}).get('id') or '')
        if not stream_id or len(stream_id) > 128:
            raise ValueError('Screen stream identity unavailable')
        relative = float(data.get('timestamp', {}).get('relative', -1))
        if not math.isfinite(relative) or relative < 0:
            raise ValueError('Screen timestamp unavailable')
        key = (sid, stream_id)
        if event.startswith('video_') and data.get('type') != 'screenshare':
            return
        if relative < self.watermarks.get(sid, relative) - self.settings.screen_frame_max_age_seconds:
            return
        if event.startswith('video_') and relative <= self.floors.get(sid, -1):
            return
        self.watermarks[sid] = max(relative, self.watermarks.get(sid, -1))
        if event in {'participant_events.screenshare_off', 'participant_events.leave'}:
            screen = self.streams.get(key)
            if screen and relative >= max(screen.stamp, screen.relative):
                screen.active = False
                screen.stamp = relative
                screen.current = screen.previous = screen.summary = None
                screen.generation = uuid4().hex
                self.emit(sid, 'screen_share_stopped', participant_id=pid, relative_timestamp=relative)
            return
        if event not in {'video_separate_h264.data', 'participant_events.screenshare_on'}:
            return
        if event.startswith('video_') and data.get('type') != 'screenshare':
            return  # Cameras never enter the video decoder or vision context.
        screen = self.streams.get(key)
        if screen and (relative < screen.stamp or relative < screen.relative):
            return
        if screen and not screen.active and relative <= screen.stamp:
            return
        if screen is None:
            if sum(stream.active for stream in self.streams.values()) >= self.settings.screen_max_streams:
                self.emit(sid, 'screen_stream_rejected', reason='capacity')
                return
            screen = Screen(pid, str(participant.get('name') or 'Unknown')[:200])
            self.streams[key] = screen
            if len(self.streams) > 64:
                for old_key in list(self.streams):
                    if not self.streams[old_key].active:
                        self.streams.pop(old_key)
                        break
        if not screen.active and sum(stream.active for stream in self.streams.values()) >= self.settings.screen_max_streams:
            self.emit(sid, 'screen_stream_rejected', reason='capacity')
            return
        if event.endswith('screenshare_on'):
            if relative > screen.stamp:
                screen.active = True
                screen.stamp = relative
                screen.generation = uuid4().hex
                screen.current = screen.previous = screen.summary = None
                self.emit(sid, 'screen_share_started', participant_id=pid, relative_timestamp=relative)
            return
        if not screen.active:
            # A later frame can recover a missing on-event, but cannot resurrect
            # an off stream with an older/equal timestamp.
            screen.active = True
            screen.generation = uuid4().hex
        if participant.get('name'):
            screen.presenter = str(participant['name'])[:200]
        encoded = data.get('buffer')
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= self.settings.screen_max_message_bytes:
            raise ValueError('Screen packet exceeds limit')
        screen.relative = relative
        try:
            self.queue.put_nowait((sid, connection, stream_id, screen.generation,
                                   encoded, relative, data.get('timestamp', {}).get('absolute'), time.monotonic()))
        except asyncio.QueueFull:
            self.dropped += 1
            screen.generation = uuid4().hex
            screen.current = screen.previous = screen.summary = None
            self.emit(sid, 'screen_packet_dropped', reason='queue_full', queue_size=self.queue.qsize())

    def decode(self, key, generation, encoded, relative, emit, allowed):
        self.prune(allowed)
        record = self.decoders.get(key)
        if record is None:
            record = (generation, Decoder(self.settings.screen_max_pixels, self.settings.screen_max_edge))
            self.decoders[key] = record
        elif record[0] != generation:
            record[1].reset()
            record = (generation, record[1])
            self.decoders[key] = record
        return record[1].decode(encoded, relative, emit)

    async def run(self):
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1)
            except TimeoutError:
                await self.cleanup()
                continue
            sid, connection, stream_id, generation, encoded, relative, absolute, received = item
            try:
                key = (sid, stream_id)
                screen = self.streams.get(key)
                if (not screen or not screen.active or screen.generation != generation
                        or self.connections.get(sid) != connection):
                    continue
                if time.monotonic() - received > self.settings.screen_packet_max_age_seconds:
                    screen.generation = uuid4().hex
                    screen.current = screen.previous = screen.summary = None
                    self.emit(sid, 'screen_packet_dropped', reason='stale_queue')
                    continue
                started = time.monotonic()
                sampling = started - screen.sampled >= self.settings.screen_sample_seconds
                result = await asyncio.get_running_loop().run_in_executor(self.executor, self.decode,
                    key, generation, encoded, relative, sampling,
                    {active_key for active_key, active in self.streams.items() if active.active})
                if not result or screen.generation != generation or self.streams.get(key) is not screen:
                    continue
                if len(result['jpeg']) > self.settings.screen_max_image_bytes:
                    raise ValueError('Decoded screen image exceeds limit')
                now = time.monotonic()
                old = screen.current
                changed = old is None or old['digest'] != result['digest']
                # Compare with this content revision's anchor, not merely the last
                # packet. Cursor/codec noise must not revoke a spoken request;
                # gradual scrolling must still eventually advance the revision.
                anchor = old.get('revision_fingerprint', old['fingerprint']) if old else None
                score = (sum(abs(a - b) for a, b in zip(anchor, result['fingerprint'])) /
                         max(len(result['fingerprint']) * 255, 1)) if old else 1
                meaningful = score >= self.settings.screen_change_threshold
                if meaningful:
                    screen.revision += 1
                    if old:
                        screen.previous = old
                    screen.summary = None
                screen.sampled = now
                screen.current = {**result, 'id': uuid4().hex, 'generation': generation,
                    'revision_fingerprint': result['fingerprint'] if meaningful else anchor,
                    'revision': screen.revision, 'participant_id': screen.participant_id,
                    'presenter': screen.presenter, 'relative_timestamp': relative,
                    'provider_absolute_timestamp': absolute, 'received_monotonic': received,
                    'decoded_monotonic': now, 'stream_id': stream_id}
                self.emit(sid, 'screen_frame_decoded', decode_ms=(now - started) * 1000,
                          arrival_to_decode_ms=(now - received) * 1000, changed=changed,
                          change_score=score, queue_size=self.queue.qsize())
                if meaningful:
                    self.emit(sid, 'screen_change_detected', detection_ms=(time.monotonic() - received) * 1000)
                    if self.on_change:
                        self.on_change(sid, stream_id)
            except Exception as exc:
                self.emit(sid, 'screen_decode_failed', error_type=type(exc).__name__)
                screen = self.streams.get((sid, stream_id))
                if screen:
                    screen.current = screen.summary = None
                    screen.generation = uuid4().hex
            finally:
                self.queue.task_done()
                if time.monotonic() - self.cleaned > 2:
                    await self.cleanup()

    async def cleanup(self):
        self.cleaned = time.monotonic()
        for sid in list(self.connections):
            meeting = await self.store.get(sid)
            if not meeting or meeting['status'] in {'left', 'done', 'failed', 'fatal'} or time.time() > meeting['expires']:
                self.connections.pop(sid, None)
                self.watermarks.pop(sid, None)
                self.floors.pop(sid, None)
                self.invalidate(sid)
                if self.on_disconnect:
                    self.on_disconnect(sid)
        for sid in list(self.telemetry):
            if sid not in self.connections:
                await self.telemetry.pop(sid).close()
        # Decoder state is touched only on its one executor thread.
        allowed = {key for key, screen in self.streams.items() if screen.active}
        if self.executor:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.prune, allowed)

    def prune(self, allowed):
        for key in list(self.decoders):
            if key not in allowed:
                self.decoders.pop(key, None)

    def select(self, sid, presenter='', previous=False):
        candidates = []
        for (session, _), screen in self.streams.items():
            if session != sid or not screen.active or not screen.current:
                continue
            if time.monotonic() - screen.current['received_monotonic'] > self.settings.screen_frame_max_age_seconds:
                continue
            if presenter and presenter.casefold() not in {str(screen.participant_id).casefold(), screen.presenter.casefold(), screen.current['stream_id'].casefold()}:
                continue
            candidates.append(screen)
        if len(candidates) != 1:
            raise ValueError('No fresh matching shared screen' if not candidates else 'Multiple screens: specify the presenter')
        screen = candidates[0]
        frame = screen.previous if previous else screen.current
        if not frame:
            raise ValueError('No previous slide retained for this screen share')
        if time.monotonic() - frame['received_monotonic'] > self.settings.screen_previous_max_age_seconds:
            raise ValueError('Previous screen is too old')
        return frame

    async def refresh(self, sid, presenter=''):
        frame = self.select(sid, presenter)
        if time.monotonic() - frame['received_monotonic'] < .25:
            return
        screen = self.streams[(sid, frame['stream_id'])]
        screen.sampled = 0  # The next real packet bypasses ordinary sampling.
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            if screen.generation != frame['generation'] or not screen.active:
                raise ValueError('Screen changed while capturing fresh context')
            if screen.current and screen.current['id'] != frame['id']:
                return
            await asyncio.sleep(.02)
        # No new media was fabricated: select() still enforces cached-frame TTL.

    def valid(self, sid, frame, previous=False):
        screen = self.streams.get((sid, frame['stream_id']))
        if not screen or not screen.active or screen.generation != frame['generation']:
            return False
        current = screen.previous if previous else screen.current
        return bool(current and current['revision'] == frame['revision'] and
                    time.monotonic() - screen.current['received_monotonic'] <= self.settings.screen_frame_max_age_seconds)

    def context(self, sid):
        items = []
        for (session, _), screen in self.streams.items():
            if session != sid or not screen.active or not screen.current:
                continue
            frame = screen.current
            fresh = time.monotonic() - frame['received_monotonic'] <= self.settings.screen_frame_max_age_seconds
            items.append({'presenter_id': screen.participant_id, 'presenter': screen.presenter,
                          'stream_id': frame['stream_id'], 'generation': screen.generation,
                          'revision': frame['revision'], 'relative_timestamp': frame['relative_timestamp'],
                          'fresh': fresh, 'frame_age_ms': round((time.monotonic() - frame['received_monotonic']) * 1000),
                          'has_previous': screen.previous is not None, 'width': frame['width'], 'height': frame['height'],
                          'cached_summary': screen.summary if fresh else None})
        return {'enabled': self.settings.screen_enabled, 'vision_enabled': self.settings.screen_enabled and self.settings.vision_enabled,
                'screens': items, 'scope': 'Screen presenter is not necessarily the voice speaker',
                'connected': sid in self.connections, 'dropped_packets': self.dropped}

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.streams.clear()
        self.connections.clear()
        self.watermarks.clear()
        self.floors.clear()
        if self.executor:
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        self.decoders.clear()
        await asyncio.gather(*(tel.close() for tel in self.telemetry.values()))
        self.telemetry.clear()
