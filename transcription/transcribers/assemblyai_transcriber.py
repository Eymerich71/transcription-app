"""AssemblyAI transcription backend (commercial, speaker diarization included)."""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import requests as _requests

LANG_CODES = {
    "English": "en",
    "Spanish": "es",
    "Italian": "it",
}

_BASE_URL = "https://api.eu.assemblyai.com"
_LLM_GATEWAY_URL = "https://llm-gateway.eu.assemblyai.com/v1/chat/completions"


def transcribe(
    audio_bytes: bytes,
    filename: str,
    language: str,
    api_key: str,
    speakers_expected: Optional[int] = None,
    generate_summary: bool = False,
) -> Dict[str, Any]:
    """Transcribe audio bytes using AssemblyAI (EU region, Universal-3 Pro)."""
    import assemblyai as aai

    if not api_key:
        raise ValueError("AssemblyAI API key is required.")

    aai.settings.api_key = api_key
    aai.settings.base_url = _BASE_URL

    lang_code = LANG_CODES.get(language, "en")
    suffix = Path(filename).suffix or ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        extra = {"speakers_expected": speakers_expected} if speakers_expected else {}
        config = aai.TranscriptionConfig(
            speech_models=["universal-3-pro", "universal-2"],
            language_code=lang_code,
            speaker_labels=True,
            **extra,
        )
        transcript = aai.Transcriber().transcribe(tmp_path, config=config)
    finally:
        os.unlink(tmp_path)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    segments = []
    if transcript.utterances:
        for utt in transcript.utterances:
            segments.append({
                "start": (utt.start or 0) / 1000.0,
                "end": (utt.end or 0) / 1000.0,
                "text": utt.text or "",
                "speaker": f"Speaker {utt.speaker}",
            })
    else:
        segments.append({
            "start": 0.0,
            "end": 0.0,
            "text": transcript.text or "",
            "speaker": "Speaker 1",
        })

    full_text = transcript.text or ""
    summary = _generate_summary(full_text, api_key) if (generate_summary and full_text) else None

    return {
        "filename": filename,
        "language": language,
        "engine": "AssemblyAI (Universal-3 Pro)",
        "segments": segments,
        "full_text": full_text,
        "summary": summary,
    }


def _generate_summary(text: str, api_key: str) -> str:
    """Summarise transcript text via AssemblyAI LLM Gateway (EU)."""
    try:
        resp = _requests.post(
            _LLM_GATEWAY_URL,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a research assistant. "
                            "Produce a concise summary of the following transcript "
                            "in 3-5 bullet points. Capture main topics, key insights, "
                            "and any decisions or action items mentioned."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "max_tokens": 600,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"Summary unavailable: {exc}"
