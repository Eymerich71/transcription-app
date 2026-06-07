"""AssemblyAI transcription backend (commercial, speaker diarization included)."""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict

LANG_CODES = {
    "English": "en",
    "Spanish": "es",
    "Italian": "it",
}


def transcribe(
    audio_bytes: bytes,
    filename: str,
    language: str,
    api_key: str,
) -> Dict[str, Any]:
    """Transcribe audio bytes using AssemblyAI with speaker diarization.

    Returns a dict with keys: filename, language, engine, segments, full_text.
    """
    import assemblyai as aai  # imported lazily

    if not api_key:
        raise ValueError("AssemblyAI API key is required.")

    aai.settings.api_key = api_key
    lang_code = LANG_CODES.get(language, "en")
    suffix = Path(filename).suffix or ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        config = aai.TranscriptionConfig(
            language_code=lang_code,
            speaker_labels=True,
        )
        transcript = aai.Transcriber().transcribe(tmp_path, config=config)
    finally:
        os.unlink(tmp_path)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    segments = []
    if transcript.utterances:
        for utt in transcript.utterances:
            segments.append(
                {
                    "start": (utt.start or 0) / 1000.0,
                    "end": (utt.end or 0) / 1000.0,
                    "text": utt.text or "",
                    "speaker": f"Speaker {utt.speaker}",
                }
            )
    else:
        # Fallback when no utterances (e.g. very short audio)
        segments.append(
            {
                "start": 0.0,
                "end": 0.0,
                "text": transcript.text or "",
                "speaker": "Speaker 1",
            }
        )

    return {
        "filename": filename,
        "language": language,
        "engine": "AssemblyAI",
        "segments": segments,
        "full_text": transcript.text or "",
    }
