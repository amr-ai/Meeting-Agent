"""Bounded workaround for the pinned Google plugin's fatal resumption collision.

LiveKit 1.8.11 treats a failed reconnect handshake as fatal, even when a stale
resume handle is the only problem. After a recoverable 1006 with a resume handle, use SDK fresh-history replay before
the next handshake to avoid the observed slow 1008 collision. Other reconnects
retain SDK resumption, and an isolated exact collision still has one bounded fallback.
The private SDK extension points below are covered by explicit contract tests.
"""
import asyncio
import base64
import logging

from google.genai import types
from livekit.agents import APIConnectionError
from livekit.plugins.google.realtime.realtime_api import RealtimeModel, RealtimeSession

from app.voice.native_tools import NATIVE_TOOL_REPLY, native_tool_context

log = logging.getLogger(__name__)


def connection_failure_reason(error: Exception) -> str:
    """Classify SDK-wrapped failures without exposing URLs, tokens or payloads."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if 'keepalive ping timeout' in str(current).casefold():
            return 'keepalive_ping_timeout'
        current = current.__cause__ or current.__context__
    return 'connection_interrupted'


def is_resume_collision(error: Exception) -> bool:
    text = str(error).casefold()
    return '1008' in text and 'session is already connected to an existing client' in text


class RecoveringRealtimeSession(RealtimeSession):
    def __init__(self, model):
        self._collision_retries = 0
        self._recover_collision = False
        self._recover_abnormal = False
        self._native_tool_bindings = {}
        self._persona_rollback = None
        self._recover_persona = False
        self._persona_failed = False
        factory = getattr(model, 'audio_filter_factory', None)
        self._input_filter = factory() if factory else None
        self._speech_timeline = getattr(model, 'speech_timeline', None)
        self._speech_timeline_failed = False
        super().__init__(model)

    async def change_persona(self, voice, instructions, deadline_seconds=12):
        """Pinned SDK transaction: same RTC session/history, fresh Gemini setup.

        A resumed provider session retains its old voice, so force fresh history replay.
        Do not report success until setup and history synchronization release the lock.
        """
        if self._persona_rollback is not None:
            raise RuntimeError('Persona transition already active')
        previous = (self._opts.voice, self._opts.instructions)
        old_session = self._active_session
        if old_session is None:
            raise RuntimeError('Voice connection is not ready')
        self._persona_rollback = previous
        self._persona_failed = False
        self._opts.voice, self._opts.instructions = voice, instructions
        self._mark_restart_needed()
        try:
            await self._wait_persona_connection(old_session, deadline_seconds)
            if self._persona_failed:
                raise RuntimeError('Persona setup rejected; original voice restored')
        except BaseException:
            if not self._persona_failed:
                self._opts.voice, self._opts.instructions = previous
                failed_session = self._active_session
                self._mark_restart_needed()
                # Rollback also has a deadline; a real network outage remains visible.
                try:
                    await asyncio.shield(self._wait_persona_connection(failed_session, deadline_seconds))
                except Exception:
                    log.warning('persona_rollback_connection_unavailable')
            raise
        finally:
            self._persona_rollback = None

    async def _wait_persona_connection(self, old_session, deadline_seconds):
        async with asyncio.timeout(deadline_seconds):
            while True:
                async with self._session_lock:
                    if (self._active_session is not None and self._active_session is not old_session
                            and not self._session_should_close.is_set()):
                        return
                if self._main_atask.done():
                    raise RuntimeError('Voice connection ended during persona switch')
                await asyncio.sleep(.05)

    def register_native_tool(self, call_id, binding):
        if call_id not in self._open_call_ids:
            return False
        self._native_tool_bindings[call_id] = binding
        return True

    def push_audio(self, frame):
        observer = getattr(self._realtime_model, 'audio_observer', None)
        if observer and not getattr(self, '_audio_observer_failed', False):
            try:
                observer(frame)
            except Exception:
                self._audio_observer_failed = True
                log.warning('audio_input_diagnostics_disabled')
        if self._input_filter is not None:
            frame = self._input_filter.process(frame)
        # Measurement of what the provider actually receives. Observes only: the frame
        # is forwarded unchanged and in order, and a failure disables diagnostics.
        if self._speech_timeline is not None and not self._speech_timeline_failed:
            try:
                self._speech_timeline.push(frame)
            except Exception:
                self._speech_timeline_failed = True
                log.warning('speech_timeline_disabled')
        super().push_audio(frame)

    def push_screen_image(self, call_id, encoded, question=''):
        if call_id not in self._native_tool_bindings or call_id not in self._open_call_ids:
            return False
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(b'\xff\xd8') or len(data) > 2000000:
            raise ValueError('Invalid or oversized screen JPEG')
        # The Live visual input channel, not a historical user image. Enqueued
        # before the real tool response on the same ordered transport.
        self._send_client_event(types.LiveClientRealtimeInput(
            video=types.Blob(data=data, mime_type='image/jpeg')))
        self._native_tool_bindings[call_id]['image_question'] = question[:1000]
        return True

    def _sync_chat_ctx(self, chat_ctx, *, known=None):
        # Normalize before the SDK computes its diff AND adopts replay history.
        # Other tools and ordinary voice/chat messages retain their existing path.
        # The voice executor's first progress output is appended directly to the
        # model context, whose original FunctionCall is a different SDK object.
        # Bind it explicitly rather than assuming executor metadata was copied.
        items = []
        for item in chat_ctx.items:
            root = getattr(item, 'call_id', '')
            binding = self._native_tool_bindings.get(root)
            if binding is None and root.endswith('_final'):
                binding = self._native_tool_bindings.get(root[:-6])
            if item.type == 'function_call' and binding:
                item = item.model_copy(update={'extra': {**item.extra, NATIVE_TOOL_REPLY: binding}})
            items.append(item)
        annotated = type(chat_ctx)(items)
        super()._sync_chat_ctx(native_tool_context(annotated),
                              known=native_tool_context(known) if known is not None else None)
        completed = {item.call_id[:-6] for item in chat_ctx.items
                     if item.type == 'function_call_output' and item.call_id.endswith('_final')}
        for root in completed:
            binding = self._native_tool_bindings.pop(root, None)
            if binding and binding.get('reply') and binding.get('image_question'):
                handler = getattr(self._realtime_model, 'visual_reply_handler', None)
                if handler:
                    handler(root, binding['image_question'], binding.get('stop_serial'), binding.get('epoch'))
                else:
                    log.warning('visual_reply_scheduler_unavailable')

    def _emit_error(self, error: Exception, recoverable: bool) -> None:
        if not recoverable and self._persona_rollback is not None and not self._persona_failed:
            # One bounded rollback before the SDK closes the AgentSession.
            self._opts.voice, self._opts.instructions = self._persona_rollback
            self._persona_failed = True
            self._recover_persona = True
            super()._emit_error(error, recoverable=True)
            return
        if (recoverable and ('1006' == str(getattr(error, 'code', '')) or
                             '1006' in str(error) and 'abnormal closure' in str(error).casefold())
                and self._session_resumption_handle is not None and not self._msg_ch.closed):
            # An abnormal close can leave Google's old connection alive. Reusing
            # its handle caused the observed slow 1008 collision handshake. Use
            # the pinned SDK's own fresh-history replay flag BEFORE its next
            # connect; it clears the handle after the old receive task is closed.
            self._fresh_session_needed = True
            self._recover_abnormal = True
            log.warning('voice_abnormal_close_reconnecting_with_retained_history')
        if (not recoverable and is_resume_collision(error) and self._collision_retries < 1
                and not self._msg_ch.closed):
            self._recover_collision = True
            # Prevent AgentSession closing before the transport can be recovered.
            super()._emit_error(error, recoverable=True)
            return
        super()._emit_error(error, recoverable=recoverable)

    def _report_connection_acquired(self, elapsed: float) -> None:
        super()._report_connection_acquired(elapsed)
        # Bound consecutive failed handshakes, not the lifetime of a meeting.
        # A second independent network drop after a successful recovery is recoverable too.
        if self._collision_retries or self._recover_abnormal:
            log.info('voice_connection_recovered')
        self._collision_retries = 0
        self._recover_abnormal = False

    async def _main_task(self) -> None:
        while True:
            try:
                await super()._main_task()
                return
            except APIConnectionError:
                if self._recover_persona and not self._msg_ch.closed:
                    self._recover_persona = False
                    await self._close_active_session()
                    self._session_resumption_handle = None
                    self._resumption_chat_ctx = None
                    self._fresh_session_needed = True
                    self._num_retries = 0
                    continue
                if not self._recover_collision or self._msg_ch.closed:
                    raise
                self._recover_collision = False
                self._collision_retries += 1
                await self._close_active_session()
                self._session_resumption_handle = None
                self._resumption_chat_ctx = None
                self._num_retries = 0
                log.warning('voice_resume_collision_recovering_with_retained_history')
                await asyncio.sleep(.75)


class RecoveringRealtimeModel(RealtimeModel):
    async def change_persona(self, voice, instructions, deadline_seconds=12):
        sessions = [session for session in self._sessions if not session._msg_ch.closed]
        if len(sessions) != 1:
            raise RuntimeError('Expected one active voice session')
        await sessions[0].change_persona(voice, instructions, deadline_seconds=deadline_seconds)
        self._opts.voice = voice

    def push_screen_image(self, call_id, encoded, question=''):
        return any(session.push_screen_image(call_id, encoded, question) for session in self._sessions)

    def register_native_tool(self, call_id, binding):
        return any(session.register_native_tool(call_id, binding) for session in self._sessions)

    def session(self, *, turn_detection_disabled: bool = False) -> RealtimeSession:
        session = RecoveringRealtimeSession(self)
        self._sessions.add(session)
        return session
