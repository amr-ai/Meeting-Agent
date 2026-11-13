"""One serialized identity transaction; ordinary audio never awaits this controller."""
import asyncio
import time

from app.agent.gating import normalize
from app.agent.personas import PERSONAS, supported_voice


class PersonaSwitch:
    def __init__(self, agent):
        self.agent = agent
        self.lock = asyncio.Lock()
        self.used_inputs = set()

    async def run(self, target, request_text, user_requested):
        a = self.agent
        if target not in PERSONAS or not user_requested:
            return {'switched': False, 'reason': 'No explicit user switch request'}
        if self.lock.locked():
            return {'switched': False, 'reason': 'Another persona switch is in progress; do not queue a second switch'}
        turns = a.turns
        # Tool calls may precede transcript final. Briefly wait for the same utterance,
        # never apply a partial such as "bring Mariam ... no, keep Khaled".
        input_id, epoch = turns.input_id, a.gate.epoch
        for _ in range(10):
            if turns.last_input_final:
                break
            await asyncio.sleep(.05)
        text = ' '.join(normalize(turns.last_input_text).split()).strip(' .!؟?،')
        evidence = ' '.join(normalize(request_text).split()).strip(' .!؟?،')
        if (not evidence or evidence != text or not turns.last_input_final
                or turns.input_id != input_id or a.gate.epoch != epoch
                or turns.last_input_epoch != epoch or time.monotonic() - turns.last_input_at > 45
                or not a.gate.tools_allowed()):
            return {'switched': False, 'reason': 'Request must match the current authorized completed utterance'}
        key = (input_id, epoch)
        if key in self.used_inputs:
            return {'switched': False, 'reason': 'This request has already been handled'}
        persona = PERSONAS[target]
        if target == a.persona.key:
            return {'switched': False, 'active_persona': a.persona.name, 'reason': 'Already active'}
        if not supported_voice(persona.voice):
            return {'switched': False, 'reason': f'Voice {persona.voice} is not supported; no substitution made'}
        async with self.lock:
            self.used_inputs.add(key)
            if len(self.used_inputs) > 64:
                self.used_inputs = {key}
            started = time.monotonic()
            previous = a.persona
            previous_instructions = a.instructions_for(previous.key)
            transport_changed = False
            a.persona_switching = True
            a.persona_generation += 1  # old buffered speech must never resume after a switch
            stop_serial = a.gate.stop_serial
            a.telemetry.emit('persona_switch_started', previous=previous.key, target=target)
            try:
                a.turns.tool_started()
                session = getattr(a, 'session', None)
                if session and session.output.audio:
                    session.output.audio.clear_buffer()
                instructions = a.instructions_for(target)
                await a.native_tool_model.change_persona(persona.voice, instructions)
                transport_changed = True
                # Public Agent API keeps SDK's agent state consistent; transport already has this value.
                await a.update_instructions(instructions)
                a.persona = persona
                a.gate.set_names(persona.names)
                if a.meeting_link:
                    a.meeting_link.record(f'persona-{time.monotonic_ns()}', 'control',
                        f'Active persona: {persona.name}; voice: {persona.voice}. Shared meeting context retained.')
                a.telemetry.emit('persona_switch_completed', persona=target,
                                 duration_ms=(time.monotonic() - started) * 1000)
                if stop_serial == a.gate.stop_serial:
                    a.turns.tool_completed(epoch)
                return {'switched': True, 'active_persona': persona.name, 'voice': persona.voice,
                        'continue_current_topic': True}
            except BaseException as exc:
                if transport_changed:
                    try:
                        await asyncio.shield(a.native_tool_model.change_persona(previous.voice, previous_instructions))
                        await a.update_instructions(previous_instructions)
                    except Exception:
                        a.telemetry.emit('persona_rollback_failed')
                a.telemetry.emit('persona_switch_failed', target=target, error_type=type(exc).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if stop_serial == a.gate.stop_serial:
                    a.turns.tool_completed(epoch)
                return {'switched': False, 'active_persona': previous.name,
                        'reason': 'Voice transition failed; previous identity retained. Do not claim success.'}
            finally:
                a.persona_switching = False
