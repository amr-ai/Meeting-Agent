from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    admin_api_key: SecretStr = Field(min_length=32)
    bridge_signing_secret: SecretStr = Field(min_length=32)
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: SecretStr
    google_api_key: SecretStr = SecretStr('')
    gemini_model: str = 'gemini-3.8-live'
    gemini_api_version: str = 'v1beta'
    gemini_silence_duration_ms: int = Field(default=300, ge=100, le=2000)
    gemini_voice: str = 'Iapetus'
    input_languages: str = 'ar,en'
    voice_noise_suppression: bool = True
    meeting_followup_seconds: float = Field(default=30, ge=0, le=120)
    meeting_followup_until_stop: bool = True
    agent_name: str = 'meeting-agent'
    assistant_name: str = 'Khaled'
    wake_names: str = 'Khaled,Khalid,خالد,يا خالد'
    prompt_path: Path = ROOT / 'prompts/system_prompt.md'
    recall_api_key: SecretStr = SecretStr('')
    recall_region: str = 'us-west-2'
    recall_webhook_secret: SecretStr = SecretStr('')
    recall_legacy_webhook_secret: SecretStr = SecretStr('')
    public_base_url: str = 'http://localhost:8000'
    ngrok_authtoken: SecretStr = SecretStr('')
    api_base_url: str = 'http://localhost:8000'
    tavily_api_key: SecretStr = SecretStr('')
    database_path: Path = ROOT / 'data/meetings.sqlite3'
    latency_path: Path = ROOT / 'data/latency.jsonl'
    retention_days: int = Field(default=30, ge=1)
    bridge_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    session_max_seconds: int = Field(default=7200, ge=600, le=14400)
    event_queue_size: int = Field(default=256, ge=1)
    # Opt-in only: receiving screens and sending images have separate cost gates.
    screen_enabled: bool = False
    vision_enabled: bool = False
    vision_passive_enabled: bool = False
    vision_model: str = ''
    screen_queue_size: int = Field(default=8, ge=1, le=32)
    screen_max_streams: int = Field(default=2, ge=1, le=4)
    screen_sample_seconds: float = Field(default=2, ge=.5, le=10)
    screen_packet_max_age_seconds: float = Field(default=2, ge=.2, le=10)
    screen_frame_max_age_seconds: float = Field(default=8, ge=1, le=30)
    screen_previous_max_age_seconds: float = Field(default=120, ge=10, le=300)
    screen_max_message_bytes: int = Field(default=4000000, ge=65536, le=8000000)
    screen_max_image_bytes: int = Field(default=2000000, ge=65536, le=4000000)
    screen_max_pixels: int = Field(default=4000000, ge=65536, le=8000000)
    screen_max_edge: int = Field(default=1920, ge=320, le=2560)
    screen_change_threshold: float = Field(default=.015, ge=.001, le=.5)
    vision_timeout_seconds: float = Field(default=20, ge=1, le=45)
    vision_passive_interval_seconds: float = Field(default=30, ge=10, le=300)
    chat_auto_reply: bool = True
    chat_model: str = 'gemini-3.5-flash-lite'
    chat_workers: int = Field(default=2, ge=1, le=4)
    chat_followup_seconds: float = Field(default=60, ge=0, le=180)
    chat_max_reply_age_seconds: float = Field(default=120, ge=10, le=600)
    chat_auto_per_minute: int = Field(default=6, ge=1, le=30)
    chat_participant_per_minute: int = Field(default=3, ge=1, le=10)
    chat_send_per_minute: int = Field(default=20, ge=1, le=60)
    chat_send_interval_seconds: float = Field(default=1, ge=0, le=10)

    @field_validator('vision_model')
    @classmethod
    def optional_vision_model(cls, value: str) -> str:
        return cls.text_model_name(value) if value else value

    @field_validator('chat_model')
    @classmethod
    def text_model_name(cls, value: str) -> str:
        import re
        if not re.fullmatch(r'[a-zA-Z0-9._-]+', value):
            raise ValueError('CHAT_MODEL must be a model identifier')
        return value

    @field_validator('input_languages')
    @classmethod
    def bilingual_input(cls, value: str) -> str:
        codes = [code.strip() for code in value.split(',') if code.strip()]
        if not codes or any(code not in {'ar', 'en'} for code in codes):
            raise ValueError('INPUT_LANGUAGES supports Arabic (ar) and English (en) only')
        return ','.join(dict.fromkeys(codes))

    @field_validator('recall_region')
    @classmethod
    def valid_region(cls, value: str) -> str:
        if value not in {'us-west-2', 'us-east-1', 'eu-central-1', 'ap-northeast-1'}:
            raise ValueError('Unsupported Recall region')
        return value

    @field_validator('gemini_model')
    @classmethod
    def live_model(cls, value: str) -> str:
        if 'live' not in value and 'native-audio' not in value:
            raise ValueError('A Gemini Live audio model is required')
        return value
