"""Thinking budget must be opt-in, and only reach the wire when configured.

Native-audio thinking is the largest single suspect behind the measured 11 s
speech-end -> first audio, but an explicit budget is only sent when it is set, because
the previously verified connection must not change by accident.
"""
from google.genai import types
from livekit.agents.types import NOT_GIVEN

from app.agent.worker import create_model


def _model(settings, **update):
    model = create_model(settings.model_copy(update=update))
    try:
        return model._opts.thinking_config
    finally:
        # create_model builds a RealtimeModel, not an open socket; nothing to close.
        pass


def test_no_budget_keeps_the_provider_default(settings):
    assert settings.gemini_thinking_budget is None
    assert _model(settings) is NOT_GIVEN


def test_an_explicit_budget_is_forwarded(settings):
    config = _model(settings, gemini_thinking_budget=0)
    assert isinstance(config, types.ThinkingConfig)
    assert config.thinking_budget == 0


def test_a_non_zero_budget_is_forwarded_unchanged(settings):
    assert _model(settings, gemini_thinking_budget=512).thinking_budget == 512
