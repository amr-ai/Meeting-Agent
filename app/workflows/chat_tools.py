from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.storage.chat_repository import ChatRepository


class ChatToolState(TypedDict, total=False):
    sid: str
    operation: str
    arguments: dict
    authorized: bool
    result: dict
    error: str


class ChatToolsWorkflow:
    """Bound to a server-trusted meeting, never to a model-selected ID.

    The graph is built once (see ``create_app`` lifespan) and the meeting is carried in
    the state, because compiling it per request cost 15-72 ms of event-loop CPU on every
    voice chat-tool call before any work started.
    """
    def __init__(self, store, sender, sid: str = ''):
        self.store, self.sender, self.sid = store, sender, sid
        self.repo = ChatRepository(store)
        graph = StateGraph(ChatToolState)
        graph.add_node('authorize', self.authorize)
        graph.add_node('execute', self.execute)
        graph.add_edge(START, 'authorize')
        graph.add_conditional_edges('authorize', lambda s: END if s.get('error') else 'execute')
        graph.add_edge('execute', END)
        self.graph = graph.compile()

    async def authorize(self, state):
        sid = state.get('sid', '')
        if not state.get('authorized'):
            return {'error': 'No addressed voice/chat operation is authorized'}
        if state['operation'] not in {'read_meeting_chat', 'search_meeting_chat', 'get_chat_context', 'send_meeting_chat', 'research_and_send_meeting_chat'}:
            return {'error': 'Unsupported chat operation'}
        if not sid or not await self.store.get(sid):
            return {'error': 'Meeting not found'}
        if any(k in state.get('arguments', {}) for k in ('meeting_id', 'bot_id', 'sid', 'to')):
            return {'error': 'Meeting IDs and recipients come from trusted application context'}
        return {}

    async def execute(self, state):
        args = state.get('arguments', {})
        sid = state.get('sid', '')
        try:
            if state['operation'] in {'send_meeting_chat', 'research_and_send_meeting_chat'}:
                key = args.get('idempotency_key')
                if not isinstance(key, str) or not 1 <= len(key) <= 180:
                    raise ValueError('A stable application-generated idempotency key is required')
                if state['operation'] == 'research_and_send_meeting_chat':
                    result = await self.repo.enqueue_research(sid, args.get('query', ''), args.get('message', ''), key)
                else:
                    result = await self.sender.send_chat_message(sid, args.get('message', ''), key)
            else:
                messages = await self.repo.history(sid, query=args.get('query', ''),
                    participant=args.get('participant', ''), since=args.get('since'), until=args.get('until'),
                    limit=args.get('limit', 10))
                if state['operation'] == 'get_chat_context' and args.get('query'):
                    combined = {m['id']: m for m in messages + await self.repo.relevant(sid, args['query'])}
                    messages = sorted(combined.values(), key=lambda m: m['event_time'])[-20:]
                # Storage preserves complete original text; model retrieval is bounded.
                result = {'messages': [{**m, 'content': m['content'][:2000]} for m in messages],
                          'coverage': 'Only messages delivered to this bot; no pre-join history is assumed'}
            return {'result': result}
        except (ValueError, KeyError, TypeError) as exc:
            return {'error': str(exc)}
        except Exception:
            return {'error': 'Chat operation failed; delivery must not be assumed'}

    async def run(self, operation, arguments=None, *, authorized=False, sid=None):
        state = {'sid': self.sid if sid is None else sid, 'operation': operation,
                 'arguments': arguments or {}, 'authorized': authorized}
        result = await self.graph.ainvoke(state)
        return {'error': result['error']} if result.get('error') else result.get('result', {})
