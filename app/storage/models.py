from typing import Any

from pydantic import BaseModel, Field


class Utterance(BaseModel):
    meeting_id: str
    bot_id: str
    participant_id: str = 'unresolved'
    speaker: str = 'Unknown'
    text: str
    start: float
    end: float | None = None
    source: str = 'recall-live'
    final: bool = True
    provider_event_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
