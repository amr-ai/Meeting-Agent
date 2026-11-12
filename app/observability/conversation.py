"""Session-scoped console display; fetches data only through authenticated APIs."""
class ConversationConsole:
    def __init__(self, emit=print):
        self.cursor = 0
        self.roster = None
        self.speakers = None
        self.emit = emit
        self.screen_state = None

    def display(self, data: dict) -> None:
        visual = data.get('visual_context')
        if visual is not None:
            current = [(s.get('presenter'), s.get('width'), s.get('height'))
                       for s in visual.get('screens', []) if s.get('fresh')]
            state = (visual.get('connected'), visual.get('vision_enabled'), tuple(current))
            if state != self.screen_state:
                if current:
                    self.emit('[Screen] Receiving: ' + ', '.join(f'{name} ({width}x{height})' for name, width, height in current))
                else:
                    self.emit('[Screen] No fresh shared-screen frames; waiting for video.')
                self.screen_state = state
        roster = tuple(sorted((str(p['id']), p['name']) for p in data['participants'] if p['present']))
        active = tuple(sorted(p['name'] for p in data['active_speakers']))
        present_names = {name for _, name in roster}
        speakers = active or tuple(name for name in (self.speakers or ()) if name in present_names)
        if roster != self.roster:
            self.emit('[Participants] ' + (', '.join(name for _, name in roster) or 'Waiting for Recall metadata'))
            self.roster = roster
        if speakers != self.speakers:
            self.emit('[Recall last speaker] ' + (', '.join(speakers) if speakers else 'Unknown / waiting for timeline'))
            self.speakers = speakers
        for row in data['messages']:
            if row['seq'] <= self.cursor:
                continue
            if row['role'] == 'control':
                label = 'Voice'
            elif row['role'] == 'assistant':
                label = row['speaker'] + ' / assistant'
            else:
                if row['speaker'] == 'Unknown' and row['source'] == 'gemini-live':
                    label = 'Live transcript / speaker pending Recall'
                else:
                    label = row['speaker'] + (' / Recall' if row['source'] == 'recall-live' else ' / live transcript')
            suffix = ' [interrupted]' if row['interrupted'] else ''
            self.emit(f"[{label}] {row['text']}{suffix}")
            self.cursor = row['seq']
        self.cursor = max(self.cursor, data['cursor'])
