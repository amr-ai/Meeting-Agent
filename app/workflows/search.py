import asyncio
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph


class SearchState(TypedDict, total=False):
    query: str
    sources: list[dict[str, str]]
    error: str | None


class SearchWorkflow:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client, self.key = client, api_key
        graph = StateGraph(SearchState)
        graph.add_node('validate', self.validate)
        graph.add_node('search', self.search)
        graph.add_edge(START, 'validate')
        graph.add_conditional_edges('validate', lambda s: END if s.get('error') else 'search')
        graph.add_edge('search', END)
        self.graph = graph.compile()

    async def validate(self, state: SearchState) -> SearchState:
        query = state['query'].strip()
        error = None
        if not 2 <= len(query) <= 500:
            error = 'Search query must contain 2 to 500 characters'
        elif not self.key:
            error = 'Search is unavailable: TAVILY_API_KEY is not configured'
        return {'query': query, 'error': error, 'sources': []}

    async def search(self, state: SearchState) -> SearchState:
        try:
            response = await self.client.post('https://api.tavily.com/search', json={
                'api_key': self.key, 'query': state['query'], 'max_results': 4,
                'search_depth': 'basic', 'include_answer': False}, timeout=8)
            response.raise_for_status()
            return {'sources': [{'title': r['title'][:200], 'url': r['url'],
                                 'snippet': r.get('content', '')[:1000]}
                                for r in response.json().get('results', [])[:4]]}
        except (httpx.HTTPError, KeyError, ValueError):
            return {'error': 'Search provider failed; no verified findings are available', 'sources': []}

    async def run(self, query: str) -> SearchState:
        try:
            async with asyncio.timeout(10):
                return await self.graph.ainvoke({'query': query})
        except TimeoutError:
            return {'query': query, 'error': 'Search timed out', 'sources': []}
