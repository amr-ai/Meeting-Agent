import re
import time

from app.agent.gating import normalize


class MentionDetector:
    def __init__(self, names: str, followup_seconds: float = 60):
        names = sorted({normalize(n.strip()) for n in names.split(',') if n.strip()}, key=len, reverse=True)
        self.name = r'(?:' + '|'.join(re.escape(n) for n in names) + ')'
        self.mention = re.compile(r'(?<!\w)@?' + self.name + r'(?!\w)')
        self.followup_seconds = followup_seconds

    def decide(self, text: str, thread: dict | None = None, event_time: float | None = None) -> str:
        text = normalize(text).strip()
        if thread and event_time is not None and event_time < thread['last_event']:
            return 'late'
        # Quoted/third-person mentions are reference data, not an invitation.
        if re.search(self.name + r'\s+(?:من\s+(?:التيم|الفريق)|قال|بيقول|كان بيقول|from\b|said\b|says\b)', text):
            return 'unrelated'
        if text.startswith(('"', '“', '«', '>')):
            return 'ambiguous'
        if self.mention.search(text):
            if re.search(r'(?<!\w)@' + self.name + r'(?!\w)', text):
                return 'direct'
            if re.match(r'^(?:(?:يا|hey|hi|hello|please)\s+)?' + self.name + r'(?!\w)', text):
                return 'direct'
            if re.search(r'(?:[?؟]|ممكن|can you|could you|please)', text) and re.search(self.name + r'[\s?!؟.,،]*$', text):
                return 'direct'
            return 'ambiguous'
        if thread and time.time() - thread['last_reply'] < self.followup_seconds:
            if event_time is not None and event_time <= thread['last_event']:
                return 'late'
            if re.match(r'^(?:طب|طيب|و\b|كمان|ليه|ازاي|انهي|ايه|هل|ممكن|وبالنسبة|what\b|which|why|how|and\b|so\b|can you|could you|do you)', text):
                return 'followup'
        return 'unrelated'
