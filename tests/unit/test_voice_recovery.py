import asyncio
import ssl

import pytest
from livekit.agents import APIConnectionError
from livekit.plugins.google.realtime.realtime_api import RealtimeSession

from app.agent.worker import create_model
from app.voice.network import tls_context, warm_network_runtime
from app.voice.recovery import connection_failure_reason, is_resume_collision

COLLISION = '1008 None. BidiGenerateContent session is already connected to an existing client'


@pytest.mark.parametrize('message,expected', [
    (COLLISION, True), ('1008 invalid API key', False),
    ('1006 abnormal closure', False), ('1007 invalid argument', False),
])
def test_only_exact_resumption_conflict_can_trigger_fallback(message, expected):
    assert is_resume_collision(RuntimeError(message)) == expected


async def test_voice_session_constructs_without_blocking_tls_initialization(settings, monkeypatch):
    await asyncio.to_thread(warm_network_runtime)
    context = tls_context()

    def forbidden(*args, **kwargs):
        pytest.fail('Voice session must reuse prewarmed certificate contexts')

    async def idle(self):
        pass

    monkeypatch.setattr(ssl, 'create_default_context', forbidden)
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    model = create_model(settings)
    session = model.session()
    try:
        # Verify the actual installed Google SDK accepted the public HttpOptions
        # and reused our verified context across HTTP, aiohttp and WebSocket.
        client = session._client._api_client
        assert client._websocket_ssl_ctx['ssl'] is context
        assert client._websocket_ssl_ctx['ping_interval'] == 10
        assert client._websocket_ssl_ctx['ping_timeout'] is None
        assert client._async_client_session_request_args['ssl'] is context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        await session._main_atask
    finally:
        await session.aclose()


async def test_stale_resume_collision_recovers_once_and_preserves_chat_history(settings, monkeypatch):
    emitted, attempts = [], []

    async def failing_then_working(self):
        attempts.append((self._session_resumption_handle, self._chat_ctx))
        if len(attempts) == 1:
            self._session_resumption_handle = 'synthetic-stale-handle'
            self._resumption_chat_ctx = self._chat_ctx.copy()
            self._emit_error(RuntimeError(COLLISION), recoverable=False)
            raise APIConnectionError('Failed to connect to Gemini Live')

    monkeypatch.setattr(RealtimeSession, '_main_task', failing_then_working)
    monkeypatch.setattr(RealtimeSession, '_emit_error',
                        lambda self, error, recoverable: emitted.append(recoverable))
    session = create_model(settings).session()
    session._chat_ctx.add_message(role='user', content='Earlier meeting question')
    try:
        await asyncio.wait_for(session._main_atask, timeout=3)
        assert len(attempts) == 2 and attempts[1][0] is None
        assert attempts[0][1] is attempts[1][1]
        assert session._chat_ctx.items[0].text_content == 'Earlier meeting question'
        assert emitted == [True] and session._collision_retries == 1
    finally:
        await session.aclose()


async def test_successful_connection_renews_budget_for_a_later_independent_drop(settings, monkeypatch):
    emitted, attempts = [], []

    async def separate_drops(self):
        attempts.append(1)
        if len(attempts) in {1, 3}:
            self._emit_error(RuntimeError(COLLISION), recoverable=False)
            raise APIConnectionError('Resume handshake conflict')
        if len(attempts) == 2:
            self._report_connection_acquired(.1)
            self._emit_error(RuntimeError(COLLISION), recoverable=False)
            raise APIConnectionError('Later independent resume conflict')
        self._report_connection_acquired(.1)

    monkeypatch.setattr(RealtimeSession, '_main_task', separate_drops)
    monkeypatch.setattr(RealtimeSession, '_emit_error',
                        lambda self, error, recoverable: emitted.append(recoverable))
    session = create_model(settings).session()
    try:
        # First conflict -> fresh success -> another conflict -> fresh attempt.
        # A third consecutive failed handshake must still remain fatal.
        with pytest.raises(APIConnectionError):
            await session._main_atask
        assert len(attempts) == 3 and emitted == [True, True, False]
    finally:
        await session.aclose()


@pytest.mark.parametrize('message,expected_attempts,expected_errors', [
    ('1008 invalid API key', 1, [False]),
    (COLLISION, 2, [True, False]),
])
async def test_recovery_is_bounded_and_does_not_hide_other_fatal_errors(
        settings, monkeypatch, message, expected_attempts, expected_errors):
    attempts, emitted = [], []

    async def failing(self):
        attempts.append(1)
        self._emit_error(RuntimeError(message), recoverable=False)
        raise APIConnectionError('Failed to connect')

    monkeypatch.setattr(RealtimeSession, '_main_task', failing)
    monkeypatch.setattr(RealtimeSession, '_emit_error',
                        lambda self, error, recoverable: emitted.append(recoverable))
    session = create_model(settings).session()
    try:
        with pytest.raises(APIConnectionError):
            await asyncio.wait_for(session._main_atask, timeout=3)
        assert len(attempts) == expected_attempts and emitted == expected_errors
    finally:
        await session.aclose()


@pytest.mark.parametrize('abnormal', [True, False])
async def test_real_sdk_reconnect_clears_abnormal_handle_before_handshake_but_preserves_clean_resume(settings, monkeypatch, abnormal):
    handles, replays, errors = [], [], []
    session = create_model(settings).session()
    session._chat_ctx.add_message(role='user', content='Earlier meeting request')

    class Remote:
        def __init__(self, number):
            self.number = number
            self.closed = asyncio.Event()

        async def close(self):
            self.closed.set()
            session._session_should_close.set()

        async def send_client_content(self, **kwargs):
            replays.append((self.number, kwargs))

        async def send_realtime_input(self, **kwargs):
            pass

    class Connection:
        async def __aenter__(self):
            self.remote = Remote(len(handles))
            return self.remote

        async def __aexit__(self, *args):
            await self.remote.close()

    def connect(*, model, config):
        handles.append(config.session_resumption.handle)
        return Connection()

    async def sending(self, remote):
        await remote.closed.wait()

    async def receiving(self, remote):
        if remote.number == 1:
            self._session_resumption_handle = 'provider-handle'
            self._resumption_chat_ctx = self._chat_ctx.copy()
            if abnormal:
                raise RuntimeError('1006 None. abnormal closure [internal]')
            self._session_should_close.set()  # A normal restart still uses resumption.
        else:
            self._msg_ch.close()

    monkeypatch.setattr(session._client.aio.live, 'connect', connect)
    monkeypatch.setattr(RealtimeSession, '_send_task', sending)
    monkeypatch.setattr(RealtimeSession, '_recv_task', receiving)
    monkeypatch.setattr(RealtimeSession, '_emit_error', lambda self, error, recoverable: errors.append(recoverable))
    try:
        await asyncio.wait_for(session._main_atask, 3)
        assert handles == [None, None if abnormal else 'provider-handle']
        assert session._chat_ctx.items[0].text_content == 'Earlier meeting request'
        if abnormal:
            assert errors == [True]  # No slow conflicting-resume handshake and no second error.
            assert any(number == 2 for number, _ in replays)
        else:
            assert not errors
    finally:
        await session.aclose()


async def test_unrelated_recoverable_error_does_not_request_fresh_history(settings, monkeypatch):
    async def idle(self):
        pass
    monkeypatch.setattr(RealtimeSession, '_main_task', idle)
    monkeypatch.setattr(RealtimeSession, '_emit_error', lambda *args, **kwargs: None)
    session = create_model(settings).session()
    session._session_resumption_handle = 'valid-handle'
    session._emit_error(RuntimeError('temporary transport failure'), recoverable=True)
    assert not session._fresh_session_needed
    assert session._session_resumption_handle == 'valid-handle'
    await session.aclose()


def test_connection_diagnostics_preserve_wrapped_ping_timeout_without_payloads():
    inner = RuntimeError('sent 1011 keepalive ping timeout; no close frame received')
    outer = RuntimeError('1006 abnormal closure https://example.com/?token=secret')
    outer.__context__ = inner
    assert connection_failure_reason(outer) == 'keepalive_ping_timeout'
    assert connection_failure_reason(RuntimeError('1006 abnormal closure')) == 'connection_interrupted'
    inner.__context__ = outer  # Defensive against unusual SDK exception chains.
    assert connection_failure_reason(outer) == 'keepalive_ping_timeout'


@pytest.mark.parametrize('ping_timeout,should_survive', [(10, False), (None, True)])
async def test_real_websocket_delayed_pong_reproduces_drop_and_survives_new_budget(ping_timeout, should_survive):
    # No Google/Recall calls: the real installed transport streams data while its
    # peer delays a pong beyond the former ten-second deadline.
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Opcode

    from app.voice.network import google_http_options

    delivered = asyncio.Event()
    async def peer(connection):
        original = connection.protocol.send_frame
        timers = []
        def delayed(frame):
            if frame.opcode != Opcode.PONG:
                original(frame)
                return
            def release():
                if connection.transport.is_closing():
                    return
                original(frame)
                connection.send_data()
                delivered.set()
            timers.append(asyncio.get_running_loop().call_later(11, release))
        connection.protocol.send_frame = delayed
        try:
            while not delivered.is_set():
                await connection.send(b'ongoing-stream')
                await asyncio.sleep(.05)
            await connection.send(b'pong-delivered')
            await connection.wait_closed()
        except ConnectionClosed:
            pass
        finally:
            for timer in timers:
                timer.cancel()

    async with serve(peer, '127.0.0.1', 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        options = google_http_options().async_client_args
        async with connect(f'ws://127.0.0.1:{port}', ping_interval=.05,
                           ping_timeout=ping_timeout, close_timeout=options['close_timeout']) as socket:
            async def read():
                async for message in socket:
                    if message == b'pong-delivered':
                        return True
                return False
            if should_survive:
                assert ping_timeout == options['ping_timeout']
                assert await asyncio.wait_for(read(), timeout=15)
                assert delivered.is_set()
            else:
                with pytest.raises(ConnectionClosed, match='keepalive ping timeout'):
                    await asyncio.wait_for(read(), timeout=15)
                assert not delivered.is_set()


async def test_idle_socket_survives_over_sixty_seconds_and_receives_next_question():
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.frames import Opcode

    from app.voice.network import google_http_options

    pings = []
    async def peer(connection):
        original = connection.protocol.send_frame
        def ignore_pongs(frame):
            if frame.opcode == Opcode.PONG:
                pings.append(frame)
            else:
                original(frame)
        connection.protocol.send_frame = ignore_pongs
        question = await connection.recv()
        await connection.send('reply:' + question)

    options = google_http_options().async_client_args
    async with serve(peer, '127.0.0.1', 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}', ping_interval=options['ping_interval'],
                           ping_timeout=options['ping_timeout'], close_timeout=options['close_timeout']) as socket:
            # Actual elapsed time, no application traffic and no peer pongs.
            await asyncio.sleep(65)
            await socket.send('are you there?')
            assert await asyncio.wait_for(socket.recv(), 3) == 'reply:are you there?'
            assert len(pings) >= 5
