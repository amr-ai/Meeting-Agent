from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.meeting.chat.models import ENDED
from app.memory.context import context


class ResearchState(TypedDict, total=False):
    job: dict
    sources: list
    answer: str
    result: dict
    error: str


class ResearchChatWorkflow:
    """Durable public-research -> compose -> Meet chat, outside the Live turn."""
    def __init__(self, store, sender, reply_workflow):
        self.store, self.sender, self.reply = store, sender, reply_workflow
        graph = StateGraph(ResearchState)
        graph.add_node('validate', self.validate)
        graph.add_node('search', self.search)
        graph.add_node('compose', self.compose)
        graph.add_node('send', self.send)
        graph.add_edge(START, 'validate')
        graph.add_conditional_edges('validate', lambda s: END if s.get('error') else
                                    ('send' if s['job'].get('answer') else 'search'))
        graph.add_conditional_edges('search', lambda s: END if s.get('error') else 'compose')
        graph.add_conditional_edges('compose', lambda s: END if s.get('error') else 'send')
        graph.add_edge('send', END)
        self.graph = graph.compile()

    async def validate(self, state):
        job = state['job']
        meeting = await self.store.get(job['meeting_id'])
        current = await self.store.rows('SELECT status FROM chat_voice_jobs WHERE id=?', (job['id'],))
        if current and current[0]['status'] == 'cancelled':
            return {'error': 'Research was cancelled'}
        if not meeting or meeting['status'] in ENDED:
            return {'error': 'Meeting ended before research could be posted'}
        if not self.reply.wants_search('search ' + job['query']):
            return {'error': 'Only public research can use this operation'}
        return {'answer': job.get('answer') or ''}

    async def search(self, state):
        stopped = await self.validate(state)
        if stopped.get('error'):
            return stopped
        result = await self.reply.search.run(state['job']['query'])
        if result.get('error') or not result.get('sources'):
            return {'error': result.get('error') or 'No verified search sources were found; nothing was posted'}
        return {'sources': result['sources']}

    async def compose(self, state):
        job = state['job']
        stopped = await self.validate(state)
        if stopped.get('error'):
            return stopped
        previous = await context(self.store, job['meeting_id'], job['query'], limit=5000)
        answer = await self.reply.responder.generate(
            {'participant_name': 'Current voice caller', 'content':
             'The caller explicitly requested public research and posting its result in Meet chat. '
             'Search query: ' + job['query'] + '\nRequested format: ' + job['instructions'] +
             '\nUse only supplied verified sources. Include a useful source link. '
             'For latest research, do not claim globally latest unless dated evidence establishes it. '
             'Keep the result under 450 characters when possible.'}, previous, state['sources'])
        await self.store.execute('UPDATE chat_voice_jobs SET answer=? WHERE id=?', (answer, job['id']))
        return {'answer': answer}

    async def send(self, state):
        stopped = await self.validate(state)
        if stopped.get('error'):
            return stopped
        job = state['job']
        return {'result': await self.sender.send_chat_message(job['meeting_id'], state['answer'],
                                                              'research:' + job['id'])}

    async def run(self, job):
        return await self.graph.ainvoke({'job': job})
