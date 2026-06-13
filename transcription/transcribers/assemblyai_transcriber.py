"""AssemblyAI transcription backend (commercial, speaker diarization included)."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests as _requests

LANG_CODES = {
    "English": "en",
    "Spanish": "es",
    "Italian": "it",
}

_BASE_URL = "https://api.eu.assemblyai.com"
_LLM_GATEWAY_URL = "https://llm-gateway.eu.assemblyai.com/v1/chat/completions"
_LLM_MODEL = "claude-haiku-4-5-20251001"


def transcribe(
    audio_bytes: bytes,
    filename: str,
    language: str,
    api_key: str,
    speakers_expected: Optional[int] = None,
    generate_summary: bool = False,
    smart_paragraphs: bool = False,
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
            punctuate=True,
            format_text=True,
            **extra,
        )
        transcript = aai.Transcriber().transcribe(tmp_path, config=config)
    finally:
        os.unlink(tmp_path)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {transcript.error}")

    segments = _build_segments(transcript, api_key, smart_paragraphs)

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


def _build_segments(transcript, api_key: str, smart_paragraphs: bool) -> List[Dict]:
    """Pick the best segmentation.

    More than one distinct speaker -> diarized utterance view. A single speaker
    -> paragraph view; if smart_paragraphs is on, regroup sentences into
    semantic paragraphs via the LLM (preserving exact words and timestamps),
    otherwise fall back to AssemblyAI's acoustic paragraphs.
    """
    utterances = transcript.utterances or []
    distinct_speakers = {u.speaker for u in utterances}

    if len(distinct_speakers) > 1:
        return _segments_from_utterances(utterances)

    if smart_paragraphs:
        smart = _smart_paragraphs(transcript, api_key)
        if smart:
            return smart

    return _segments_from_paragraphs(transcript)


def _segments_from_utterances(utterances) -> List[Dict]:
    """Build segments from speaker-diarized utterances (multi-speaker path)."""
    return [
        {
            "start": (utt.start or 0) / 1000.0,
            "end": (utt.end or 0) / 1000.0,
            "text": utt.text or "",
            "speaker": f"Speaker {utt.speaker}",
        }
        for utt in utterances
    ]


def _segments_from_paragraphs(transcript) -> List[Dict]:
    """Build segments from AssemblyAI acoustic paragraphs (single-speaker path)."""
    try:
        paragraphs = transcript.get_paragraphs()
    except Exception:
        paragraphs = []

    if paragraphs:
        return [
            {
                "start": (p.start or 0) / 1000.0,
                "end": (p.end or 0) / 1000.0,
                "text": p.text or "",
                "speaker": "Speaker 1",
            }
            for p in paragraphs
        ]

    return [
        {
            "start": 0.0,
            "end": 0.0,
            "text": transcript.text or "",
            "speaker": "Speaker 1",
        }
    ]


def _smart_paragraphs(transcript, api_key: str) -> Optional[List[Dict]]:
    """Regroup sentences into semantic paragraphs via LLM Gateway.

    Sends numbered sentences and asks the model to return groupings of sentence
    indices only (compact output, no rewriting). Paragraphs are reconstructed
    from the original sentences, so words and timestamps are exact. Returns None
    on any failure so the caller can fall back to acoustic paragraphs.
    """
    try:
        sentences = transcript.get_sentences()
    except Exception:
        sentences = []
    if not sentences:
        return None

    n = len(sentences)
    numbered = "\n".join(f"{i}: {s.text}" for i, s in enumerate(sentences))

    groups = _llm_group_sentences(numbered, n, api_key)
    if not groups:
        return None

    segments = []
    for grp in groups:
        first, last = grp[0], grp[-1]
        text = " ".join(sentences[i].text for i in grp)
        segments.append({
            "start": (sentences[first].start or 0) / 1000.0,
            "end": (sentences[last].end or 0) / 1000.0,
            "text": text,
            "speaker": "Speaker 1",
        })
    return segments or None


def _llm_group_sentences(numbered: str, n: int, api_key: str) -> Optional[List[List[int]]]:
    """Ask the LLM to group sentence indices into paragraphs. Returns a complete,
    ordered partition of 0..n-1, or None on failure."""
    system = (
        "You receive numbered sentences from a transcript, one per line. "
        "Group consecutive sentences into coherent semantic paragraphs based on "
        "topic and conversational flow. Keep sentences in their original order. "
        "Return ONLY a JSON array of arrays of integers (the sentence numbers), "
        "for example [[0,1,2],[3,4,5]]. No prose, no markdown, no explanation."
    )
    try:
        resp = _requests.post(
            _LLM_GATEWAY_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "model": _LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": numbered},
                ],
                "max_tokens": 4000,
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        raw = _extract_json_array(content)
        if not raw:
            return None

        # Build a clean, ordered partition: keep first occurrence of each valid
        # index; append any sentences the model dropped so none are lost.
        seen = set()
        groups: List[List[int]] = []
        for g in raw:
            grp = [i for i in g if isinstance(i, int) and 0 <= i < n and i not in seen]
            seen.update(grp)
            if grp:
                groups.append(grp)
        if not groups:
            return None
        missing = [i for i in range(n) if i not in seen]
        if missing:
            groups.append(missing)
        return groups
    except Exception:
        return None


def _extract_json_array(content: str):
    """Pull the first JSON array out of an LLM response (tolerant of fences)."""
    if not content:
        return None
    match = re.search(r"\[.*\]", content, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _generate_summary(text: str, api_key: str) -> str:
    """Summarise transcript text via AssemblyAI LLM Gateway (EU)."""
    try:
        resp = _requests.post(
            _LLM_GATEWAY_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "model": _LLM_MODEL,
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
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = ""
        try:
            body = (exc.response.text or "")[:300]
        except Exception:
            pass
        if status == 401:
            return (
                "Summary unavailable (HTTP 401). Your API key works for "
                "transcription but not for LLM Gateway — it's a separate product. "
                "Check that LLM Gateway is enabled and billing is set up in your "
                "AssemblyAI dashboard. " + (f"Details: {body}" if body else "")
            )
        return f"Summary unavailable (HTTP {status}). {body}"
    except Exception as exc:
        return f"Summary unavailable: {exc}"
