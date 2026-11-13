"""Actual in-memory H264 decode and authenticated mocked Recall transport."""
import base64
from fractions import Fraction

import av
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.integrations.recall import RecallClient
from app.main import create_app
from app.meeting.security import sign_bridge, sign_video
from app.screen.decoder import Decoder, crop_image, image_frame
from app.screen.receiver import Screens
from tests.integration.test_api import FakeLiveKit, FakeRecall


def h264_packets():
    encoder = av.CodecContext.create('libx264', 'w')
    encoder.width, encoder.height, encoder.pix_fmt = 160, 96, 'yuv420p'
    encoder.time_base = Fraction(1, 10)
    encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'g': '1'}
    result = []
    for index in range(6):
        pixels = np.full((96, 160, 3), index * 35, dtype=np.uint8)
        pixels[20:60, 30:100, :] = 255 - index * 30
        frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
        frame.pts = index
        result.extend(bytes(p) for p in encoder.encode(frame))
    result.extend(bytes(p) for p in encoder.encode(None))
    return result


def event(sid, packet, relative=1, pid='7', name='Ahmed', kind='screenshare', event_type='video_separate_h264.data'):
    return {'event': event_type, 'data': {'bot': {'id': 'bot-1'}, 'video_separate': {'id': 'stream'}, 'data': {
        'type': kind, 'participant': {'id': pid, 'name': name},
        'timestamp': {'relative': relative, 'absolute': '2026-11-09T10:00:00Z'},
        'buffer': base64.b64encode(packet).decode()}}}


def test_actual_h264_decode_jpeg_resolution_and_crop():
    decoder = Decoder()
    frames = [f for index, packet in enumerate(h264_packets())
              if (f := decoder.decode(base64.b64encode(packet).decode(), index))]
    assert frames and all(frame['width'] == 160 and frame['height'] == 96 for frame in frames)
    assert frames[0]['digest'] != frames[-1]['digest']
    assert frames[0]['jpeg'].startswith(b'\xff\xd8')
    cropped = image_frame(crop_image(frames[-1]['jpeg'], (0, 0, .5, .5), 4000000, 1920), 4000000)
    assert cropped.width == 80 and cropped.height == 48
    with pytest.raises(ValueError):
        decoder.decode('not base64', 1)
    with pytest.raises(ValueError):
        crop_image(frames[-1]['jpeg'], (.8, .2, .1, .9), 4000000, 1920)


async def test_receiver_filters_cameras_tracks_presenters_off_and_reconnect(store, settings):
    db, sid = store
    settings = settings.model_copy(update={'screen_enabled': True})
    screens = Screens(db, settings)
    await screens.start()
    try:
        connection = screens.connect(sid)
        packets = h264_packets()
        screens.ingest(sid, connection, event(sid, packets[0], kind='webcam'))
        assert not screens.streams
        for index, packet in enumerate(packets):
            if (sid, '7') in screens.streams:
                screens.streams[(sid, '7')].sampled = 0
            screens.ingest(sid, connection, event(sid, packet, index + 1))
            await screens.queue.join()
        frame = screens.select(sid)
        assert frame['participant_id'] == '7' and frame['presenter'] == 'Ahmed'
        assert frame['provider_absolute_timestamp'] == '2026-11-09T10:00:00Z'
        assert screens.valid(sid, frame)
        screens.ingest(sid, connection, event(sid, b'', 20, event_type='participant_events.screenshare_off'))
        assert not screens.valid(sid, frame)
        screens.ingest(sid, connection, event(sid, packets[0], 19))
        assert not screens.context(sid)['screens']
        new_connection = screens.connect(sid)
        screens.disconnect(sid, connection)  # Old socket cannot invalidate its replacement.
        assert screens.connections[sid] == new_connection
        screens.ingest(sid, connection, event(sid, packets[0], 21))
        assert not screens.streams
    finally:
        await screens.close()
    assert screens.task.done() and not screens.decoders


async def test_backpressure_invalidates_old_content_and_bounds_queue(store, settings):
    db, sid = store
    screens = Screens(db, settings.model_copy(update={'screen_enabled': True, 'screen_queue_size': 1}))
    connection = screens.connect(sid)
    packet = h264_packets()[0]
    screens.ingest(sid, connection, event(sid, packet, 1))
    first_generation = screens.streams[(sid, '7')].generation
    screens.ingest(sid, connection, event(sid, packet, 2))
    assert screens.queue.qsize() == 1 and screens.dropped == 1
    assert screens.streams[(sid, '7')].generation != first_generation
    assert screens.streams[(sid, '7')].current is None
    await screens.close()


async def test_recall_video_configuration_is_opt_in_and_never_enables_separate_audio(settings):
    bodies = []
    async def endpoint(request):
        import json
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={'id': 'bot'})
    from uuid import uuid4
    sid = str(uuid4())
    bridge = settings.public_base_url + '/bridge#' + sign_bridge(sid, settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        await RecallClient(http, settings).create('https://meet.google.com/abc-defg-hij', bridge)
        enabled = settings.model_copy(update={'screen_enabled': True})
        await RecallClient(http, enabled).create('https://meet.google.com/abc-defg-hij', bridge)
    assert 'video_separate_h264' not in bodies[0]['recording_config']
    assert bodies[1]['recording_config']['video_separate_h264'] == {}
    assert all('audio_separate_raw' not in body['recording_config'] for body in bodies)
    endpoint = bodies[1]['recording_config']['realtime_endpoints'][-1]
    assert endpoint['url'].startswith('wss://agent.example/ws/recall-video/' + sid + '/?token=')
    assert bodies[1]['variant']['google_meet'] == 'web_4_core'


def test_websocket_capabilities_bot_binding_size_and_disconnect(settings):
    enabled = settings.model_copy(update={'screen_enabled': True})
    app = create_app(enabled, FakeLiveKit(), FakeRecall())
    auth = {'Authorization': 'Bearer ' + settings.admin_api_key.get_secret_value()}
    with TestClient(app) as client:
        joined = client.post('/api/meetings', headers=auth, json={
            'meeting_url': 'https://meet.google.com/abc-defg-hij', 'consent_confirmed': True}).json()
        sid = joined['session_id']
        url = f'/ws/recall-video/{sid}/?token='
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url + sign_bridge(sid, enabled)):
                pass
        with client.websocket_connect(url + sign_video(sid, enabled)) as socket:
            packet = event(sid, h264_packets()[0])
            packet['data']['bot']['id'] = 'different-bot'
            socket.send_json(packet)
            with pytest.raises(WebSocketDisconnect):
                socket.receive_text()
        assert client.get(f'/api/meetings/{sid}/screens').status_code == 401
        assert not client.get(f'/api/meetings/{sid}/screens', headers=auth).json()['connected']


def test_screen_socket_is_disabled_in_working_baseline(settings):
    app = create_app(settings, FakeLiveKit(), FakeRecall())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/recall-video/not-a-session/?token=x'):
                pass


def test_h264_fragmented_buffers_are_parsed_without_losing_nal_continuations():
    decoder = Decoder()
    raw = b''.join(h264_packets())
    outputs = []
    for index, offset in enumerate(range(0, len(raw), 127)):
        frame = decoder.decode(base64.b64encode(raw[offset:offset + 127]).decode(), index / 10)
        if frame:
            outputs.append(frame)
    assert outputs and outputs[-1]['width'] == 160
