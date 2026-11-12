"""Credentialed native Gemini Live preflight without recording a microphone."""
import argparse
import asyncio

from google import genai
from google.genai import types

from app.config.settings import Settings


async def check(settings: Settings, generate_audio: bool = False) -> bool:
    client = genai.Client(api_key=settings.google_api_key.get_secret_value(),
        http_options=types.HttpOptions(api_version=settings.gemini_api_version))
    try:
        async with asyncio.timeout(25):
            async with client.aio.live.connect(model=settings.gemini_model,
                    config=types.LiveConnectConfig(
                        response_modalities=[types.Modality.AUDIO],
                        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_voice))),
                        input_audio_transcription=types.AudioTranscriptionConfig(),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        context_window_compression=types.ContextWindowCompressionConfig(
                            sliding_window=types.SlidingWindow()),
                        session_resumption=types.SessionResumptionConfig())) as session:
                print(f'Gemini Live connected: {settings.gemini_model} / {settings.gemini_api_version}', flush=True)
                if generate_audio:
                    await session.send_client_content(turns=types.Content(role='user', parts=[
                        types.Part(text='Say only: Ready to help.')]), turn_complete=True)
                    async for message in session.receive():
                        if message.data and any(message.data):
                            print(f'Native non-silent audio received: {len(message.data)} bytes. '
                                  'This confirms generation, not browser playback.', flush=True)
                            return True
                    print('The model completed without non-silent audio.', flush=True)
                    return False
                return True
    except Exception as exc:
        response = getattr(exc, 'response', None)
        code = getattr(response, 'status_code', None) or getattr(exc, 'code', None)
        # Do not print provider exception strings: some include authenticated URLs.
        print(f'Gemini Live check failed: {type(exc).__name__}, code={code}. '
              'Check GEMINI_API_VERSION, GEMINI_MODEL, GOOGLE_API_KEY and quota.', flush=True)
        return False
    finally:
        await client.aio.aclose()
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-audio', action='store_true',
                        help='Also request a short native audio reply (uses model quota)')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(Settings(), args.generate_audio)) else 1)


if __name__ == '__main__':
    main()
