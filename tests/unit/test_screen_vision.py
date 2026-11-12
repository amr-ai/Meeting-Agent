"""Vision infrastructure tests; no real images are sent to external providers."""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app.screen.decoder import Decoder, image_frame
from app.screen.receiver import Screen, Screens
from app.screen.vision import GeminiVision, Vision
from tests.unit.test_screen_reception import h264_packets


def seed(screens, sid, pid='7', presenter='Ahmed'):
    decoder = Decoder()
    frames = [frame for index, packet in enumerate(h264_packets())
              if (frame := decoder.decode(base64.b64encode(packet).decode(), index))]
    screen = Screen(pid, presenter)
    screen.revision = 2
    for index, frame in enumerate(frames[-2:]):
        frame.update(id=str(index), stream_id=pid, generation=screen.generation, revision=index + 1,
                     participant_id=pid, presenter=presenter, relative_timestamp=index + 1,
                     provider_absolute_timestamp='2026-11-09T10:00:00Z', received_monotonic=time.monotonic())
    screen.previous, screen.current = frames[-2:]
    screens.streams[(sid, pid)] = screen
    return screen


class Provider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.calls = []

    async def analyze(self, images, prompt):
        self.calls.append((images, json.loads(prompt)))
        self.started.set()
        await self.release.wait()
        return {'answer': 'Visible content explained', 'readable': True,
                'visible_text': 'ExampleError: missing module', 'search_query': 'Python ExampleError missing module'}


async def test_on_demand_mode_never_uploads_changed_frames_until_a_question(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True,
                                      'vision_passive_enabled': False})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    try:
        for _ in range(5):
            screens.on_change(sid, '7')
        await asyncio.sleep(0)
        assert not provider.calls and not vision.jobs and not vision.passive
        result = await vision.analyze(sid, 'What is on the shared screen?')
        assert result['status'] == 'completed' and len(provider.calls) == 1
        for _ in range(5):
            screens.on_change(sid, '7')
        await asyncio.sleep(0)
        assert len(provider.calls) == 1 and not vision.passive
    finally:
        await vision.close()
        await screens.close()


@pytest.mark.parametrize('question', ['Explain the Python code', 'Summarize this slide',
                                     'Read this error', 'Explain this chart', 'Read the documentation diagram'])
async def test_visual_question_payload_and_attributed_result(store, settings, question):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    try:
        result = await vision.analyze(sid, question, meeting_context='Mohamed is asking; Ahmed is presenting')
        assert result['status'] == 'completed' and result['source']['presenter'] == 'Ahmed'
        assert provider.calls[0][1]['meeting_reference_data'].startswith('Mohamed')
        assert provider.calls[0][0][0].startswith(b'\xff\xd8')
        assert len(await db.rows("SELECT * FROM memory WHERE kind='visual_summary'")) == 1
        assert all(job['frame'] is None for job in vision.jobs.values())
    finally:
        await vision.close()
        await screens.close()


async def test_switch_during_inference_never_returns_old_current_screen(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    screen = seed(screens, sid)
    provider = Provider()
    provider.release.clear()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    pending = asyncio.create_task(vision.analyze(sid, 'What is this error?'))
    await provider.started.wait()
    screen.generation = 'new-screen-generation'
    screen.current = None
    provider.release.set()
    result = await pending
    assert result['status'] == 'failed' and 'changed' in result['error']
    assert not screen.summary
    await vision.close()
    await screens.close()


async def test_multiple_presenters_previous_slide_crop_and_stale_protection(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    first = seed(screens, sid)
    seed(screens, sid, '8', 'Sara')
    provider = Provider()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    result = await vision.analyze(sid, 'Explain the screen')
    assert 'Multiple screens' in result['error'] and not provider.calls
    result = await vision.analyze(sid, 'Compare slides', presenter='Ahmed', compare=True, crop=(0, 0, .5, .5))
    assert result['status'] == 'completed' and len(provider.calls[0][0]) == 2
    assert image_frame(provider.calls[0][0][0], 4000000).width == 80
    first.current['received_monotonic'] -= 60
    result = await vision.analyze(sid, 'Explain', presenter='Ahmed')
    assert 'fresh' in result['error'] and len(provider.calls) == 1
    await vision.close()
    await screens.close()


async def test_gemini_actual_configured_model_is_probed_before_private_images(settings):
    requests = []
    async def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': json.dumps({
            'answer': 'Red' if len(requests) == 1 else 'Please zoom', 'readable': len(requests) == 1,
            'visible_text': '', 'search_query': ''})}]}}]})
    cfg = settings.model_copy(update={'vision_model': 'custom-image-model'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        provider = GeminiVision(http, cfg)
        image = GeminiVision.probe_image()
        result = await provider.analyze([image], 'private meeting question')
        assert provider.verified and not result['readable']
        assert len(requests) == 2 and 'Capability probe' in requests[0]['contents'][0]['parts'][0]['text']
        assert 'private meeting question' not in str(requests[0])
        assert 'tools' not in requests[1]
        await provider.analyze([image], 'another question')
        assert len(requests) == 3  # Probe reused, not billed on each frame/question.


async def test_unavailable_image_model_sends_only_synthetic_probe(settings):
    requests = []
    async def endpoint(request):
        requests.append(request)
        return httpx.Response(400, json={'error': {'message': 'image input not supported'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        provider = GeminiVision(http, settings.model_copy(update={'vision_model': 'custom-unverified-model'}))
        with pytest.raises(httpx.HTTPStatusError):
            await provider.analyze([b'private-image-not-to-send'], 'private question')
        assert len(requests) == 1 and 'private-image-not-to-send' not in requests[0].content.decode()
        with pytest.raises(ValueError):
            await provider.analyze([b'private-image-not-to-send'], 'private question')
        assert len(requests) == 1


async def test_on_demand_preempts_background_and_shutdown_cancels_safely(store, settings):
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    screens.connect(sid)
    seed(screens, sid)
    provider = Provider()
    provider.release.clear()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    background = asyncio.create_task(vision.analyze(sid, 'Topic summary', background=True))
    await provider.started.wait()
    requested = asyncio.create_task(vision.analyze(sid, 'Explain this error'))
    await asyncio.sleep(.02)
    provider.release.set()
    assert (await background)['status'] == 'cancelled'
    assert (await requested)['status'] == 'completed'
    provider.release.clear()
    provider.started.clear()
    pending = asyncio.create_task(vision.analyze(sid, 'Another question'))
    await provider.started.wait()
    await vision.close()
    assert 'error' in await pending
    assert not vision.jobs and vision.task.done()
    await screens.close()


async def test_minor_frame_updates_preserve_spoken_reference_and_inflight_analysis(store, settings, monkeypatch):
    from tests.unit.test_screen_reception import event
    db, sid = store
    cfg = settings.model_copy(update={'screen_enabled': True, 'vision_enabled': True})
    screens = Screens(db, cfg)
    await screens.start()
    connection = screens.connect(sid)
    original = seed(screens, sid).current.copy()
    screens.streams.clear()
    value = [0]
    def decode(*args):
        return {**original, 'digest': str(value[0]), 'fingerprint': bytes([value[0]]) * 576}
    monkeypatch.setattr(screens, 'decode', decode)
    async def receive(level):
        value[0] = level
        if (sid, '7') in screens.streams:
            screens.streams[(sid, '7')].sampled = 0
        screens.ingest(sid, connection, event(sid, b'fixture', relative=level + 1))
        await screens.queue.join()
    provider = Provider()
    provider.release.clear()
    vision = Vision(screens, None, cfg, provider)
    await vision.start()
    pending = None
    try:
        await receive(0)
        first = screens.select(sid)
        expected = {key: first[key] for key in ('stream_id', 'generation', 'revision')}
        await receive(1)  # Different bytes, below the meaningful-change threshold.
        pending = asyncio.create_task(vision.analyze(sid, 'Describe the screen', expected=expected))
        await asyncio.wait_for(provider.started.wait(), 2)
        await receive(2)  # Keep streaming while the provider processes its snapshot.
        assert screens.valid(sid, first)
        provider.release.set()
        result = await asyncio.wait_for(pending, 2)
        assert result['status'] == 'completed' and len(provider.calls) == 1
        assert result['source']['revision'] == first['revision']
        await receive(3)
        assert screens.valid(sid, first)
        await receive(4)  # Small adjacent steps accumulate against the anchored image.
        assert not screens.valid(sid, first)
        assert screens.select(sid)['revision'] == first['revision'] + 1
        rejected = await vision.analyze(sid, 'Describe the old screen', expected=expected)
        assert 'changed since the spoken question' in rejected['error']
        assert len(provider.calls) == 1
    finally:
        provider.release.set()
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await vision.close()
        await screens.close()


async def test_known_image_model_uses_one_request_without_extra_billable_probe(settings):
    requests = []
    async def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': json.dumps({
            'answer': 'A code editor is visible.', 'readable': True, 'visible_text': '', 'search_query': ''})}]}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        provider = GeminiVision(http, settings.model_copy(update={'vision_model': 'gemini-3.5-flash-lite'}))
        result = await provider.analyze([b'synthetic-test-image'], 'Describe the screen')
        assert result['readable'] and len(requests) == 1
        assert 'Capability probe' not in str(requests[0])
        assert len(requests[0]['contents'][0]['parts']) == 2
