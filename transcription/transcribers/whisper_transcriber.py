"""Local Whisper transcription backend (free, no API key needed)."""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict

LANG_CODES = {
    "English": "en",
    "Spanish": "es",
    "Italian": "it",
}

_model_cache: Dict[str, Any] = {}


def _load_model(model_size: str):
    if model_size not in _model_cache:
        import whisper
        _model_cache[model_size] = whisper.load_model(model_size)
    return _model_cache[model_size]


def transcribe(
    audio_bytes: bytes,
    filename: str,
    language: str,
    model_size: str = "base",
) -> Dict[str, Any]:
    model = _load_model(model_size)
    lang_code = LANG_CODES.get(language, "en")
    suffix = Path(filename).suffix or ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        result = model.transcribe(tmp_path, language=lang_code)
    finally:
        os.unlink(tmp_path)

    segments = [
        {
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
            "speaker": "Speaker 1",
        }
        for seg in result.get("segments", [])
    ]

    return {
        "filename": filename,
        "language": language,
        "engine": f"Whisper ({model_size})",
        "segments": segments,
        "full_text": result.get("text", "").strip(),
    }
