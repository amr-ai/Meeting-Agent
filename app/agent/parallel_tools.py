"""Use LiveKit's public async-tool lifecycle without blocking the voice turn."""
import asyncio
import re

from livekit.agents.llm import ToolResult

from app.voice.native_tools import NATIVE_TOOL_REPLY


class ParallelTools:
    def __init__(self, agent):
        self.agent = agent
        self.calls = {}
        self.inline = {}
        self.completion_serial = 0

    def prune(self):
        # Keep active calls and a bounded window for late SDK notifications.
        completed = [key for key, value in self.calls.items() if value['status'] != 'running']
        completed.sort(key=lambda key: self.calls[key].get('completion_serial', 0))
        for key in completed[:-256]:
            del self.calls[key]

    @staticmethod
    def matches_update(call_id, update_id):
        # Match the pinned SDK's IDs without accepting arbitrary prefix collisions.
        if update_id == call_id:
            return True
        suffix = update_id.removeprefix(call_id)
        return suffix == '_final' or bool(re.fullmatch(r'_update_\d+', suffix))

    async def run(self, context, name, progress, work, *, silent=False, native_reply=False):
        agent = self.agent
        if not agent.gate.tools_allowed():
            return {'error': 'No addressed tool operation is authorized'}
        call_id = context.function_call.call_id
        self.prune()
        record = {'name': name, 'status': 'running', 'stop_serial': agent.gate.stop_serial,
                  'epoch': agent.gate.epoch}
        self.calls[call_id] = record
        native = native_reply and getattr(agent, 'native_tool_replies', False)
        binding = {'root': call_id, 'reply': False, 'stop_serial': agent.gate.stop_serial,
                   'epoch': agent.gate.epoch}
        model = getattr(agent, 'native_tool_model', None)
        if native and model is not None:
            native = model.register_native_tool(call_id, binding)
            if not native:
                agent.telemetry.emit('native_tool_binding_unavailable', operation=name)
        if native:
            if not hasattr(context.function_call, 'extra'):
                context.function_call.extra = {}
            context.function_call.extra[NATIVE_TOOL_REPLY] = binding
        # This is the original addressed turn's acknowledgment, not a stale result.
        agent.turns.tool_started()
        agent.turns.tool_completed(agent.gate.epoch)
        try:
            # Public SDK API: first update releases the conversation; the remaining
            # coroutine belongs to the async-tool executor, not pending voice tasks.
            await context.update(progress, silent=True if native else silent)
            result = await work()
            record['status'] = 'done'
            agent.telemetry.emit('parallel_tool_completed', operation=name)
            if record['stop_serial'] != agent.gate.stop_serial or agent.gate.silenced:
                # Keep the result in context, but don't restart speech after "stop".
                return ToolResult(result.output if isinstance(result, ToolResult) else result, reply_required=False)
            if native:
                if record['epoch'] != agent.gate.epoch:
                    # Do not start an explicit visual answer turn over a newer
                    # question. Let the provider schedule the result when idle.
                    binding.pop('image_question', None)
                binding['reply'] = not isinstance(result, ToolResult) or result.reply_required
                if binding['reply']:
                    agent.gate.authorize_background_reply()
                    agent.turns.awaiting_tool_reply = True
                    agent.turns.tool_serial += 1
                    agent.telemetry.emit('native_tool_reply_ready', operation=name)
                # Gemini speaks from the real result when idle. Don't also ask
                # the SDK to generate a second synthetic async reply.
                return ToolResult(result.output if isinstance(result, ToolResult) else result, reply_required=False)
            return result
        except BaseException as error:
            record['status'] = 'cancelled_or_failed'
            if (native and not isinstance(error, asyncio.CancelledError)
                    and record['stop_serial'] == agent.gate.stop_serial and not agent.gate.silenced):
                binding['reply'] = True
                agent.gate.authorize_background_reply()
                agent.turns.awaiting_tool_reply = True
                agent.turns.tool_serial += 1
            raise
        finally:
            self.completion_serial += 1
            record['completion_serial'] = self.completion_serial
            self.prune()

    def on_event(self, event):
        update = event.update
        if update.type == 'tool_call_started' and update.function_call.name in {'lk_agents_get_running_tasks', 'lk_agents_cancel_task'}:
            if self.agent.gate.tools_allowed():
                self.inline[update.function_call.call_id] = self.agent.gate.epoch
                self.agent.turns.tool_started()
            return
        if update.type == 'tool_call_ended' and update.call_id in self.inline:
            self.agent.turns.tool_completed(self.inline.pop(update.call_id))
            return
        if update.type != 'tool_reply_updated' or update.status != 'scheduled':
            return
        # The SDK coalesces completed results and schedules them only when idle.
        # Accept only replies originating from our explicitly authorized calls.
        gate = self.agent.gate
        if gate.silenced:
            return
        records = []
        for call_id, record in self.calls.items():
            if record['stop_serial'] != gate.stop_serial:
                continue
            seen = record.setdefault('reply_updates', set())
            fresh = {entry for entry in update.update_ids
                     if self.matches_update(call_id, entry) and entry not in seen}
            if fresh:
                seen.update(fresh)
                records.append(record)
        if not records:
            self.agent.telemetry.emit('parallel_reply_ignored', reason='unknown_stale_or_duplicate_update')
            return
        gate.authorize_background_reply()
        self.agent.turns.awaiting_tool_reply = True
        self.agent.turns.tool_serial += 1
        self.agent.telemetry.emit('parallel_result_reply_scheduled', tasks=len(records))
