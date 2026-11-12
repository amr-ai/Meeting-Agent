import asyncio
import random
from urllib.parse import urlparse

import httpx

from app.config.settings import Settings
from app.meeting.security import sign_video, validate_meeting_url, verify_bridge


class RecallClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client, self.settings = client, settings
        self.base = f'https://{settings.recall_region}.recall.ai/api/v1'

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        # Retry safe GETs and explicit pre-acceptance capacity errors only. An ambiguous
        # POST timeout must be inspected by an operator rather than creating a second bot.
        for attempt in range(3):
            try:
                response = await self.client.request(method, self.base + path, json=payload,
                    headers={'Authorization': self.settings.recall_api_key.get_secret_value()}, timeout=20)
            except httpx.TransportError:
                if method != 'GET' or attempt == 2:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in {429, 503, 507} and attempt < 2:
                delay = response.headers.get('Retry-After', '')
                await asyncio.sleep(min(float(delay), 10) if delay.isdigit()
                                    else random.uniform(0.3, 0.8) * 2**attempt)
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError('Recall request exhausted retries')

    async def create(self, meeting_url: str, bridge_url: str) -> dict:
        s = self.settings
        if not s.recall_api_key.get_secret_value() or not s.recall_webhook_secret.get_secret_value():
            raise ValueError('Recall API key and workspace webhook secret are required')
        if urlparse(s.public_base_url).scheme != 'https':
            raise ValueError('Meeting mode requires a public HTTPS base URL')
        payload = {
            'meeting_url': validate_meeting_url(meeting_url), 'bot_name': s.assistant_name,
            'output_media': {'camera': {'kind': 'webpage', 'config': {'url': bridge_url}}},
            'variant': {'google_meet': 'web_4_core', 'zoom': 'web_4_core',
                        'microsoft_teams': 'web_4_core'},
            'recording_config': {
                'participant_events': {},
                'include_bot_in_recording': {'audio': False},
                'transcript': {'provider': {'recallai_streaming': {
                    'language_code': 'auto', 'mode': 'prioritize_accuracy'}}},
                'realtime_endpoints': [{'type': 'webhook',
                    'url': s.public_base_url.rstrip('/') + '/webhooks/recall/',
                    'events': ['transcript.data', 'transcript.partial_data',
                               'participant_events.join', 'participant_events.update',
                               'participant_events.leave', 'participant_events.speech_on',
                               'participant_events.speech_off', 'participant_events.chat_message']}]}}
        if s.screen_enabled:
            sid = verify_bridge(urlparse(bridge_url).fragment, s)
            websocket_origin = s.public_base_url.rstrip('/').replace('https://', 'wss://', 1)
            config = payload['recording_config']
            config.update({'video_mixed_layout': 'gallery_view_v2', 'video_separate_h264': {}})
            config['realtime_endpoints'].append({'type': 'websocket',
                'url': websocket_origin + f'/ws/recall-video/{sid}/?token=' + sign_video(sid, s),
                'events': ['video_separate_h264.data', 'participant_events.screenshare_on',
                           'participant_events.screenshare_off', 'participant_events.leave']})
        return await self.request('POST', '/bot/', payload)

    async def leave(self, bot_id: str) -> None:
        await self.request('POST', f'/bot/{bot_id}/leave_call/')

    async def status(self, bot_id: str) -> dict:
        return await self.request('GET', f'/bot/{bot_id}/')

    async def final_transcript(self, bot_id: str) -> list[dict] | None:
        bot = await self.status(bot_id)
        for recording in reversed(bot.get('recordings', [])):
            transcript = recording.get('media_shortcuts', {}).get('transcript')
            if not transcript or transcript.get('status', {}).get('code') != 'done':
                continue
            url = transcript.get('data', {}).get('download_url')
            if not url:
                continue
            # This URL comes exclusively from the authenticated provider response.
            if urlparse(url).scheme != 'https':
                raise ValueError('Insecure provider transcript URL')
            response = await self.client.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        return None


    async def send_chat_message(self, bot_id: str, message: str) -> dict:
        from app.meeting.chat.models import split_message
        if split_message(message) != [message]:
            raise ValueError('Split Google Meet messages before calling Recall')
        response = await self.client.post(self.base + f'/bot/{bot_id}/send_chat_message/',
            headers={'Authorization': self.settings.recall_api_key.get_secret_value()},
            json={'to': 'everyone', 'message': message}, timeout=15)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def final_chat_events(self, bot_id: str) -> list[dict] | None:
        bot = await self.status(bot_id)
        recordings = bot.get('recordings') or []
        if not recordings:
            return None
        result = []
        for recording in recordings:
            artifact = recording.get('media_shortcuts', {}).get('participant_events') or {}
            if artifact.get('status', {}).get('code') != 'done':
                return None
            url = artifact.get('data', {}).get('participant_events_download_url')
            if not url:
                return None
            if urlparse(url).scheme != 'https':
                raise ValueError('Insecure provider participant-events URL')
            response = await self.client.get(url, timeout=30)
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError('Unsupported participant event artifact schema')
            result.extend(e for e in events if e.get('action') == 'chat_message')
        return result
