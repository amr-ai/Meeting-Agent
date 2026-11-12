"""Check isolated RTC microphone delivery without Gemini, tools or a meeting bot."""
import asyncio
import json
import math
import time
from array import array
from datetime import timedelta
from uuid import uuid4

from livekit import api, rtc

from app.config.settings import ROOT, Settings


async def main():
    settings = Settings()
    room_name = 'rtc-transport-probe-' + uuid4().hex
    client = api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key,
                            settings.livekit_api_secret.get_secret_value())
    sender, receiver = rtc.Room(), rtc.Room()
    heard = asyncio.Event()
    readers = []
    result = {'scope': 'Isolated LiveKit RTC synthetic microphone to receiver; no Gemini or Meet',
              'subscribed': False, 'received_frames': 0, 'audible_frames': 0}

    def token(identity):
        return (api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
            .with_identity(identity).with_ttl(timedelta(minutes=5))
            .with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt())

    async def read(track):
        stream = rtc.AudioStream(track, sample_rate=24000, num_channels=1)
        try:
            async for event in stream:
                result['received_frames'] += 1
                if max(abs(value) for value in event.frame.data) > 500:
                    result['audible_frames'] += 1
                    heard.set()
        finally:
            await stream.aclose()

    @receiver.on('track_subscribed')
    def subscribed(track, publication, participant):
        result['subscribed'] = True
        readers.append(asyncio.create_task(read(track)))

    try:
        await client.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=60))
        await asyncio.gather(sender.connect(settings.livekit_url, token('synthetic-source')),
                             receiver.connect(settings.livekit_url, token('synthetic-receiver')))
        source = rtc.AudioSource(24000, 1, queue_size_ms=40)
        track = rtc.LocalAudioTrack.create_audio_track('synthetic-tone', source)
        await sender.local_participant.publish_track(track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        for index in range(500):
            pcm = array('h', [int(7000 * math.sin(2 * math.pi * 440 * (index * 480 + i) / 24000)) for i in range(480)])
            await source.capture_frame(rtc.AudioFrame(pcm.tobytes(), 24000, 1, 480))
            await asyncio.sleep(.02)
            if heard.is_set():
                break
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await asyncio.gather(sender.disconnect(), receiver.disconnect())
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        await client.aclose()
        result['utc_finished'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (ROOT / 'docs/RTC_TRANSPORT_PROBE.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
    if not heard.is_set():
        raise RuntimeError('RTC microphone subscription did not deliver audible frames')


if __name__ == '__main__':
    asyncio.run(main())
