import asyncio

import httpx
import pytest
import respx

from app.workflows.search import SearchWorkflow


@respx.mock
async def test_real_langgraph_routes_request_and_returns_sources():
    route = respx.post('https://api.tavily.com/search').mock(return_value=httpx.Response(200,
        json={'results': [{'title': 'Official docs', 'url': 'https://example.org', 'content': 'Evidence'}]}))
    async with httpx.AsyncClient() as client:
        workflow = SearchWorkflow(client, 'test-key')
        result = await workflow.run('LiveKit native audio')
    assert route.call_count == 1
    assert result['sources'][0]['snippet'] == 'Evidence'
    assert 'validate' in workflow.graph.get_graph().nodes


@respx.mock
async def test_search_validation_and_provider_failure_are_explicit():
    async with httpx.AsyncClient() as client:
        assert (await SearchWorkflow(client, '').run('search something'))['error']
        route = respx.post('https://api.tavily.com/search').mock(return_value=httpx.Response(503))
        workflow = SearchWorkflow(client, 'test-key')
        assert (await workflow.run('x'))['error']
        assert route.call_count == 0
        result = await workflow.run('proper query')
        assert result['error'] and not result['sources']


async def test_search_cancellation_is_not_converted_to_success():
    async def slow(request):
        await asyncio.sleep(60)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        task = asyncio.create_task(SearchWorkflow(client, 'key').run('cancel this search'))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
