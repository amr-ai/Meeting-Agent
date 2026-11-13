import json

import httpx
import respx

from app.integrations.recall import RecallClient


@respx.mock
async def test_recall_media_flow_payload_and_final_download(settings):
    base = 'https://us-west-2.recall.ai/api/v1'
    create = respx.post(base + '/bot/').mock(return_value=httpx.Response(200, json={'id': 'bot-1'}))
    respx.get(base + '/bot/bot-1/').mock(return_value=httpx.Response(200, json={'recordings': [{
        'media_shortcuts': {'transcript': {'status': {'code': 'done'},
        'data': {'download_url': 'https://media.example/transcript.json'}}}}]}))
    media = respx.get('https://media.example/transcript.json').mock(return_value=httpx.Response(200, json=[]))
    async with httpx.AsyncClient() as http:
        client = RecallClient(http, settings)
        await client.create('https://meet.google.com/abc-defg-hij', 'https://agent.example/bridge#signed')
        payload = json.loads(create.calls[0].request.content)
        assert payload['variant']['google_meet'] == 'web_4_core'
        assert payload['recording_config']['transcript']['provider']['recallai_streaming']['language_code'] == 'auto'
        assert payload['output_media']['camera']['config']['url'].endswith('#signed')
        assert await client.final_transcript('bot-1') == []
        assert 'authorization' not in media.calls[0].request.headers


async def test_ambiguous_create_timeout_does_not_retry(settings):
    calls = []

    async def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('ambiguous', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        try:
            await RecallClient(http, settings).create('https://meet.google.com/abc-defg-hij',
                                                      'https://agent.example/bridge#signed')
        except httpx.ReadTimeout:
            pass
    assert len(calls) == 1
