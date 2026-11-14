"""Probe whether this model accepts an explicit native-audio thinking budget.

Opt-in and credentialed: it opens one Gemini Live connection and generates no audio,
but it does touch the provider. Run it before trusting GEMINI_THINKING_BUDGET:

    python -m scripts.check_thinking --budget 0

Exit code 0 means the server accepted the config; 1 means it rejected it (the message
names the error type only, never a URL or token).
"""
import argparse
import asyncio

from google import genai
from google.genai import types

from app.config.settings import Settings


async def probe(settings: Settings, budget: int) -> bool:
    client = genai.Client(api_key=settings.google_api_key.get_secret_value(),
        http_options=types.HttpOptions(api_version=settings.gemini_api_version))
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_voice))),
        generation_config=types.GenerationConfig(thinking_config=types.ThinkingConfig(
            thinking_budget=budget)),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig())
    try:
        async with asyncio.timeout(25):
            async with client.aio.live.connect(model=settings.gemini_model, config=config):
                print(f'Accepted: {settings.gemini_model} with thinking_budget={budget}', flush=True)
                return True
    except Exception as exc:  # provider exceptions can embed authenticated URLs
        code = getattr(getattr(exc, 'response', None), 'status_code', None) or getattr(exc, 'code', None)
        print(f'Rejected: {type(exc).__name__}, code={code}. '
              f'Leave GEMINI_THINKING_BUDGET unset for this model.', flush=True)
        return False
    finally:
        await client.aio.aclose()
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget', type=int, default=0,
                        help='Thinking budget to probe (0 disables thinking)')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(probe(Settings(), args.budget)) else 1)


if __name__ == '__main__':
    main()
