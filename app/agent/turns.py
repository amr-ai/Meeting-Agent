"""Distinguish Gemini generation notifications from actual transcribed user turns."""
import time
from collections import OrderedDict

from app.agent.gating import normalize


class VoiceTurns:
    # This bounds an unconfirmed activity hint, NOT a conversation or session.
    candidate_timeout = 5.0

    def __init__(self, agent):
        self.agent = agent
        self.input_id = None
        self.awaiting_transcript = False
        self.awaiting_tool_reply = False
        self.tool_serial = 0
        self.last_input_text = ''
        self.last_input_at = 0.0
        self.last_input_epoch = -1
        self.last_input_final = False
        self.pending_semantic_stop = None
        self.seen_inputs = OrderedDict()

    @property
    def awaiting_transcript(self):
        if self._awaiting_transcript and time.monotonic() >= self.candidate_deadline:
            self.finish_candidate('transcript_timeout')
        return self._awaiting_transcript

    @awaiting_transcript.setter
    def awaiting_transcript(self, value):
        self._awaiting_transcript = value
        self.candidate_deadline = time.monotonic() + self.candidate_timeout if value else 0

    def finish_candidate(self, reason):
        if self._awaiting_transcript:
            self.awaiting_transcript = False
            self.agent.telemetry.emit('speech_candidate_closed', reason=reason,
                                      epoch=self.agent.gate.epoch)

    def provider_stopped(self):
        self.finish_candidate('activity_ended_without_transcript')

    @staticmethod
    def stop_text(text):
        return ' '.join(normalize(text).strip(' ,.!?،؟').split())

    def consume_semantic_stop(self, event):
        if not getattr(event, 'is_final', False) or self.pending_semantic_stop is None:
            return False
        candidate = self.pending_semantic_stop
        self.pending_semantic_stop = None
        # A late final may add "don't stop" or continue a partial command. Only
        # apply the model's decision to the same completed utterance it evaluated.
        return (candidate == (self.input_id, self.agent.gate.epoch, self.stop_text(event.transcript))
                and self.last_input_epoch == self.agent.gate.epoch
                and time.monotonic() - self.last_input_at <= 45
                and self.agent.gate.tools_allowed())

    def provider_started(self):
        if self.agent.gate.silenced:
            return
        # Gemini also emits this on model/tool continuations. It is not a reliable
        # physical microphone boundary and must not cancel/revoke its own tool work.
        if (self.agent.pending_tools or self.awaiting_tool_reply) and self.agent.gate.tools_allowed():
            self.agent.telemetry.emit('tool_continuation_preserved')
            return
        if not self.awaiting_transcript:
            # Activity notifications can be noise or a model continuation. They
            # must not revoke current audio or mint a fresh response permit.
            self.agent.gate.decided.clear()
            self.awaiting_transcript = True
            self.agent.telemetry.emit('speech_candidate_started', epoch=self.agent.gate.epoch)

    def transcribed(self, event):
        text = event.transcript.strip()
        if not any(char.isalnum() for char in text):
            return False
        item_id = getattr(event, 'item_id', None)
        final = getattr(event, 'is_final', True)
        signature = (text, final)
        if item_id is not None:
            previous = self.seen_inputs.get(item_id)
            # Replayed finals and late partials must never become a new request.
            if previous == signature or (previous and previous[1]):
                self.agent.telemetry.emit('duplicate_input_ignored')
                return False
            self.seen_inputs[item_id] = signature
            self.seen_inputs.move_to_end(item_id)
            if len(self.seen_inputs) > 256:
                self.seen_inputs.popitem(last=False)
            if previous is not None and item_id != self.input_id:
                # A revised/finalized OLD utterance is memory, not a new request.
                # Do not touch current intent, stop evidence, tools or candidates.
                if final and (link := getattr(self.agent, 'meeting_link', None)):
                    link.record(item_id, 'user', text)
                self.agent.telemetry.emit('historical_transcript_received', input_id=item_id,
                                          is_final=final, epoch=self.agent.gate.epoch)
                return False
        new_input = (item_id != self.input_id if item_id is not None
                     else self.awaiting_transcript or not self.last_input_text)
        self.last_input_text = text
        self.last_input_at = time.monotonic()
        self.last_input_final = final
        self.awaiting_transcript = False
        if new_input:
            self.agent.gate.reset()
            self.agent.telemetry.begin_turn()
            self.input_id = item_id
            self.awaiting_tool_reply = False
            self.pending_semantic_stop = None
            self.agent.telemetry.emit('utterance_accepted', input_id=item_id,
                                      epoch=self.agent.gate.epoch)
            for task in tuple(self.agent.pending_tools):
                task.cancel()
        self.last_input_epoch = self.agent.gate.epoch
        return True

    def tool_started(self):
        self.agent.gate.commit_turn()
        self.awaiting_tool_reply = True

    def tool_completed(self, epoch):
        if self.agent.gate.rearm_tool_reply(epoch):
            self.tool_serial += 1
            self.awaiting_tool_reply = True
            self.agent.telemetry.emit('tool_reply_authorized')

    def audio_finished(self, epoch, serial):
        if epoch == self.agent.gate.epoch and serial == self.tool_serial and not self.agent.pending_tools:
            self.awaiting_tool_reply = False
            gate = self.agent.gate
            if gate.turn_committed and gate.authorized and not gate.silenced:
                gate.active_until = time.monotonic() + gate.followup_seconds
