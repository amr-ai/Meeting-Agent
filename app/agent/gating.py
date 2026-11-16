import asyncio
import re
import time
import unicodedata


def normalize(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if unicodedata.category(c) != 'Mn')


class ResponseGate:
    """Conservative one-utterance authorization, separate from model instructions."""
    def __init__(self, names: str, local: bool = False, decision_timeout: float = 5.0,
                 followup_seconds: float = 0, followup_until_stop: bool = False,
                 permit_seconds: float = 45.0):
        self.set_names(names)
        self.local = local
        self.decision_timeout = decision_timeout
        self.permit_seconds = permit_seconds
        self.claimed = False
        self.turn_committed = False
        self.named_this_turn = False
        self.silenced = False
        self.stop_serial = 0
        self.followup_seconds = followup_seconds
        self.followup_until_stop = followup_until_stop
        self.conversation_active = False
        self.active_until = 0.0
        self.owner_id: str | None = None
        self.reported_speaker_id: str | None = None
        self.speaker_observed = 0.0
        self.stop_pattern = re.compile(
            r'(?:خلاص|اسكت|تسكت|كفايه|كفاية|وقف الكلام|بلاش تكمل|متكملش|ما تكملش|متتكلمش|ما تتكلمش|'
            r'خليك سامع|اسمع بس|شكرا كده|بس كده|تمام كده|stop talking|stop speaking|be quiet|'
            r'that[’\x27]s enough|that is enough|enough now)')
        self.epoch = 0
        self.authorized = local
        self.available = local
        self.deadline = 0.0
        self.decided = asyncio.Event()
        self.last_claim_reason = 'no_decision'

    def set_names(self, names: str) -> None:
        variants = [re.escape(normalize(n.strip())) for n in names.split(',') if n.strip()]
        self.pattern = re.compile(r'(?<!\w)(?:' + '|'.join(variants) + r')(?!\w)')

    def reset(self) -> None:
        self.epoch += 1
        if self.local:
            self.silenced = False
        self.authorized = (self.local and not self.silenced) or self.followup_allowed()
        self.available = self.authorized
        self.deadline = ((time.monotonic() + self.permit_seconds if self.followup_until_stop else self.active_until)
                         if self.authorized else 0.0)
        self.decided.clear()
        self.claimed = False
        self.turn_committed = False
        self.named_this_turn = False

    def has_wake_name(self, text: str) -> bool:
        return bool(self.pattern.search(normalize(text)))

    def set_reported_speaker(self, participant_id: str) -> None:
        self.reported_speaker_id = participant_id
        self.speaker_observed = time.monotonic()

    def followup_allowed(self) -> bool:
        if self.silenced:
            return False
        if self.followup_until_stop:
            if not self.conversation_active:
                return False
        elif self.followup_seconds <= 0 or time.monotonic() >= self.active_until:
            return False
        if self.owner_id is None:
            return self.reported_speaker_id != '__multiple__'
        # Recall speech-off leaves the last verified identity unchanged. Metadata
        # age alone must not close the activated conversation.
        # A different/multiple reported speaker still needs a fresh wake name.
        return self.reported_speaker_id == self.owner_id

    def transcript(self, text: str, *, final: bool = True) -> bool:
        named = self.has_wake_name(text)
        if self.silenced and named:
            self.silenced = False
            self.claimed = False
        if named:
            self.conversation_active = True
            self.named_this_turn = True
            self.owner_id = self.reported_speaker_id if time.monotonic() - self.speaker_observed < 15 else None
            if self.owner_id == '__multiple__':
                self.owner_id = None
            self.active_until = time.monotonic() + self.followup_seconds
        followup = self.followup_allowed()
        addressed = ((self.local and not self.silenced) or named or followup or
                     (self.turn_committed and self.authorized and not self.silenced))
        # Partial input is cumulative. Absence of a name is not a final rejection.
        if not addressed and not final:
            if not self.local and not self.named_this_turn:
                self.authorized = self.available = False
            return self.authorized
        self.authorized = addressed
        self.available = self.authorized and not self.claimed
        self.deadline = time.monotonic() + self.permit_seconds
        if addressed and (named or followup):
            self.active_until = time.monotonic() + self.followup_seconds
        self.decided.set()
        return self.authorized

    def tools_allowed(self) -> bool:
        allowed = not self.silenced and (self.local or (self.authorized and time.monotonic() < self.deadline))
        if allowed and not self.local and not self.named_this_turn and not self.turn_committed:
            return self.followup_allowed()
        return allowed

    def denial_reason(self) -> str:
        if self.silenced:
            return 'explicit_stop'
        if self.claimed:
            return 'permit_consumed'
        if self.authorized and time.monotonic() >= self.deadline and not self.local:
            return 'permit_expired'
        if self.reported_speaker_id == '__multiple__' and not self.named_this_turn:
            return 'overlapping_speakers'
        if self.owner_id and self.reported_speaker_id != self.owner_id and not self.named_this_turn:
            return 'different_reported_speaker'
        return 'not_addressed'

    def commit_turn(self) -> bool:
        if not self.tools_allowed():
            return False
        self.turn_committed = True
        return True

    def rearm_tool_reply(self, epoch: int) -> bool:
        if epoch != self.epoch or self.silenced or not self.authorized:
            return False
        self.turn_committed = True
        self.available = True
        self.claimed = False
        self.deadline = time.monotonic() + self.permit_seconds
        self.decided.set()
        return True

    def authorize_background_reply(self) -> bool:
        if self.silenced:
            return False
        self.authorized = self.available = self.turn_committed = True
        self.claimed = False
        self.deadline = time.monotonic() + self.permit_seconds
        self.decided.set()
        return True

    def is_stop_request(self, text: str, *, speaking: bool = False, final: bool = True) -> bool:
        text = normalize(text).strip()
        if not text or len(text.split()) > 14:
            return False
        if not self.local:
            # A meeting stop must be a COMPLETE direct command containing the
            # assistant's name. A cumulative partial may continue with "كمل شرحك".
            if not final or any(char in text for char in '\"“”«»'):
                return False
            names = self.pattern.pattern.removeprefix(r'(?<!\w)(?:').removesuffix(r')(?!\w)')
            name = r'(?:' + names + r')'
            polite = r'(?:(?:لو سمحت|من فضلك|ممكن|please|can you|could you)\s+)*'
            vocative = r'(?:يا\s*(?:عم\s+)?)?'
            stop = self.stop_pattern.pattern
            ending = r'(?:\s+(?:بقى|بقي|دلوقتي|دلوقتى|لو سمحت|من فضلك|please|now|for now))*'
            command = rf'(?:{stop})(?:\s+(?:و\s*)?{stop})*'
            text = ' '.join(re.sub(r'[،,.!?؟]+', ' ', text).split())
            before = rf'{polite}{vocative}{name}\s+{polite}{command}{ending}'
            after = rf'{polite}{command}\s+{polite}{vocative}{name}{ending}'
            return bool(re.fullmatch(rf'(?:{before}|{after})', text))
        named = bool(self.pattern.search(text))
        if not (self.local or named or self.tools_allowed() or self.followup_allowed()):
            return False
        # Match a direct command, not an occurrence inside a request, quotation,
        # negation or a polite thank-you. Speaking state is never stop intent.
        stripped = self.pattern.sub('', text)
        stripped = re.sub(r'[،,.!?؟]+', ' ', stripped)
        stripped = ' '.join(stripped.split())
        prefix = r'(?:(?:يا|يا عم|ممكن|لو سمحت|من فضلك|please|can you|could you)\s+)*'
        suffix = r'(?:\s+(?:بقي|بقى|دلوقتي|دلوقتى|شويه|شوية|لو سمحت|من فضلك|شكرا|please|now|for now))*'
        command = rf'{prefix}{self.stop_pattern.pattern}(?:\s+(?:و\s*)?{self.stop_pattern.pattern})*{suffix}'
        if not re.fullmatch(command, stripped):
            return False
        # "خلاص ..." can continue as "خلاص ابحثلي عن ..." in a cumulative
        # transcript. Only unambiguous stop imperatives may interrupt on a partial.
        strong = r'(?<!\w)(?:اسكت|تسكت|وقف الكلام|بلاش تكمل|متكملش|ما تكملش|متتكلمش|ما تتكلمش|خليك سامع|اسمع بس|stop talking|stop speaking|be quiet)(?!\w)'
        return final or bool(re.search(strong, stripped))

    def silence(self) -> None:
        self.epoch += 1
        self.silenced = True
        self.conversation_active = False
        self.stop_serial += 1
        self.turn_committed = False
        self.named_this_turn = False
        self.active_until = 0
        self.owner_id = None
        self.authorized = self.available = False
        self.claimed = True
        self.deadline = 0
        self.decided.set()

    def decide(self) -> tuple[bool, str]:
        """One-shot decision for a single generation. Synchronous: never waits.

        The output gate used to await a transcript here, for up to 5 s, *after* the
        model had already generated audio. The provider emits the final transcript only
        when generation ends (realtime_api.py:1587-1603), so that wait could never be
        satisfied in time; its only outcomes were added latency and a silently dropped
        answer (26 of 193 generated responses never reached the caller).

        Three outcomes are distinguished, because "no evidence yet" is not "denied":
        a generation that starts before any transcript exists is cancelled and counted,
        and it never mutates the permit state the next turn will inherit.
        """
        if self.silenced:
            self.last_claim_reason = 'explicit_stop'
            return False, self.last_claim_reason
        if not self.decided.is_set() and not self.local:
            self.last_claim_reason = 'undetermined'
            return False, self.last_claim_reason
        if not (self.available and self.tools_allowed()):
            self.last_claim_reason = self.denial_reason()
            return False, self.last_claim_reason
        self.available = False
        self.claimed = True
        self.turn_committed = True
        self.last_claim_reason = 'authorized'
        return True, self.last_claim_reason

    async def claim(self) -> tuple[bool, int]:
        """Blocking variant of decide(), kept for callers that can afford to wait.

        NOT used by the audio path: waiting there is what caused the 5 s stall.
        """
        if self.local and not self.silenced and not self.decided.is_set():
            self.decided.set()
        epoch = self.epoch
        stop_serial = self.stop_serial
        try:
            await asyncio.wait_for(self.decided.wait(), timeout=self.decision_timeout)
        except TimeoutError:
            self.last_claim_reason = 'transcript_decision_timeout'
            return False, epoch
        # Bind waiting audio to the accepted transcript, not an activity hint.
        if stop_serial != self.stop_serial:
            self.last_claim_reason = 'stop_invalidated_wait'
            return False, self.epoch
        allowed, _ = self.decide()
        return allowed, self.epoch
