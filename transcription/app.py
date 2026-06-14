import io
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import streamlit as st

load_dotenv()  # reads transcription/.env if present

st.set_page_config(
    page_title="Audio Transcription",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .main-title { font-size: 2.4rem; font-weight: 700; margin-bottom: 0.25rem; }
    .subtitle { color: #666; margin-bottom: 2rem; font-size: 1.05rem; }
    .segment-card {
        background: #f8f9fa;
        border-left: 3px solid #0066cc;
        padding: 0.6rem 1rem;
        margin: 0.4rem 0;
        border-radius: 0 4px 4px 0;
    }
    .speaker-label {
        font-weight: 700;
        color: #0066cc;
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .timestamp {
        color: #aaa;
        font-size: 0.75rem;
        font-family: monospace;
        margin-left: 0.5rem;
    }
    .seg-text { margin-top: 0.2rem; line-height: 1.5; }
    .step-header { font-size: 1.25rem; font-weight: 600; margin: 1.5rem 0 0.75rem 0; }
    .summary-box {
        background: #eef4ff;
        border-left: 3px solid #0066cc;
        padding: 0.8rem 1rem;
        border-radius: 0 4px 4px 0;
        margin-bottom: 1rem;
    }
</style>
""",
    unsafe_allow_html=True,
)

LANGUAGES = ["English", "Spanish", "Italian"]
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large"]
WHISPER_MODEL_INFO = {
    "tiny": "~39M params · fastest · lowest accuracy",
    "base": "~74M params · fast · good accuracy",
    "small": "~244M params · balanced",
    "medium": "~769M params · high accuracy · 5 GB RAM",
    "large": "~1.5B params · best accuracy · 10 GB RAM",
}
FORMATS = {
    "txt": "Plain Text (.txt)",
    "docx": "Word (.docx)",
    "pdf": "PDF (.pdf)",
    "md": "Markdown (.md)",
}
MIME = {
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "md": "text/markdown",
}
# Accepted upload types. Video containers are supported too — both Whisper
# (via ffmpeg) and AssemblyAI extract the audio track automatically.
UPLOAD_TYPES = [
    "mp3", "wav", "m4a", "flac", "ogg", "aac",
    "mp4", "webm", "mov", "mkv", "avi", "wmv", "m4v", "mpeg", "mpg",
]


def _init():
    for key, default in [
        ("transcriptions", []),
        ("speaker_map", {}),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default


def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return "0:00:00"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _all_speakers(transcriptions: List[Dict]) -> List[str]:
    seen: set = set()
    result = []
    for t in transcriptions:
        for seg in t.get("segments", []):
            sp = seg.get("speaker", "Speaker 1")
            if sp not in seen:
                seen.add(sp)
                result.append(sp)
    return result


def _apply_map(transcriptions: List[Dict], speaker_map: Dict[str, str]) -> List[Dict]:
    out = []
    for t in transcriptions:
        tc = dict(t)
        tc["segments"] = [
            {**s, "speaker": speaker_map.get(s["speaker"], s["speaker"])}
            for s in t.get("segments", [])
        ]
        out.append(tc)
    return out


def _transcribe(
    audio_bytes: bytes,
    filename: str,
    engine: str,
    language: str,
    whisper_model: Optional[str],
    api_key: Optional[str],
    speakers_expected: Optional[int],
    generate_summary: bool,
    smart_paragraphs: bool,
) -> Dict[str, Any]:
    if engine == "Whisper (Free, Local)":
        from transcribers.whisper_transcriber import transcribe
        return transcribe(audio_bytes, filename, language, whisper_model or "base")
    else:
        from transcribers.assemblyai_transcriber import transcribe
        return transcribe(
            audio_bytes, filename, language, api_key or "",
            speakers_expected=speakers_expected,
            generate_summary=generate_summary,
            smart_paragraphs=smart_paragraphs,
        )


def _export(transcription: Dict, speaker_map: Dict, fmt: str) -> bytes:
    from exporters.export_utils import export_txt, export_docx, export_pdf, export_md
    fn = {"txt": export_txt, "docx": export_docx, "pdf": export_pdf, "md": export_md}[fmt]
    return fn(transcription, speaker_map)


def _export_summary(transcription: Dict, fmt: str) -> bytes:
    from exporters.export_utils import (
        export_summary_txt, export_summary_docx, export_summary_pdf, export_summary_md,
    )
    fn = {
        "txt": export_summary_txt,
        "docx": export_summary_docx,
        "pdf": export_summary_pdf,
        "md": export_summary_md,
    }[fmt]
    return fn(transcription)


def _build_zip(transcriptions: List[Dict], speaker_map: Dict, fmt: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in transcriptions:
            if t.get("error"):
                continue
            data = _export(t, speaker_map, fmt)
            zf.writestr(f"{Path(t['filename']).stem}.{fmt}", data)
    buf.seek(0)
    return buf.read()


def _build_full_zip(transcriptions: List[Dict], speaker_map: Dict) -> bytes:
    """ZIP with every format for every file, plus a summary file per format
    when a summary is available."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in transcriptions:
            if t.get("error"):
                continue
            stem = Path(t["filename"]).stem
            for fmt in FORMATS:
                zf.writestr(f"{stem}/{stem}.{fmt}", _export(t, speaker_map, fmt))
                if t.get("summary"):
                    zf.writestr(f"{stem}/{stem}_summary.{fmt}", _export_summary(t, fmt))
    buf.seek(0)
    return buf.read()


def render_sidebar():
    env_key = os.environ.get("ASSEMBLYAI_API_KEY", "")

    with st.sidebar:
        st.header("⚙️ Settings")

        engine = st.selectbox(
            "Engine",
            ["Whisper (Free, Local)", "AssemblyAI (Commercial)"],
            help=(
                "**Whisper** runs entirely on your machine — free, private, no API key needed. "
                "**AssemblyAI** uses the EU region (Universal-3 Pro model) with automatic speaker diarization."
            ),
        )

        language = st.selectbox("Language", LANGUAGES)

        whisper_model = None
        api_key = None
        speakers_expected = None
        generate_summary = False
        smart_paragraphs = False

        if engine == "Whisper (Free, Local)":
            st.markdown("---")
            st.subheader("Whisper")
            whisper_model = st.selectbox("Model size", WHISPER_MODELS, index=1)
            st.caption(WHISPER_MODEL_INFO[whisper_model])
            st.info(
                "Speaker diarization is not available with local Whisper. "
                "Switch to AssemblyAI for automatic multi-speaker detection.",
                icon="ℹ️",
            )
        else:
            st.markdown("---")
            st.subheader("AssemblyAI")
            api_key = st.text_input(
                "API Key",
                value=env_key,
                type="password",
                placeholder="xxxxxxxxxxxxxxxxxxxxxxxx",
                help="Pre-filled from ASSEMBLYAI_API_KEY in your .env file. Get a key at assemblyai.com.",
            )
            if not api_key:
                st.warning("Enter your AssemblyAI API key to continue.")
            elif env_key and api_key == env_key:
                st.caption("🔒 Key loaded from .env")

            n = st.number_input(
                "Expected number of speakers",
                min_value=0,
                max_value=20,
                value=0,
                step=1,
                help="Set to 0 for automatic detection. Providing the correct count improves diarization accuracy.",
            )
            speakers_expected = int(n) if n > 0 else None

            generate_summary = st.checkbox(
                "Generate AI summary after transcription",
                value=False,
                help="Uses AssemblyAI LLM Gateway (EU) to produce a 3-5 bullet-point summary of each transcript.",
            )

            smart_paragraphs = st.checkbox(
                "Smart paragraph grouping (AI)",
                value=False,
                help=(
                    "Single-speaker only. Uses LLM Gateway to regroup sentences into "
                    "semantic paragraphs instead of splitting on audio pauses. "
                    "Words and timestamps are preserved. Adds one LLM call per file (billed as tokens)."
                ),
            )

        st.markdown("---")
        st.caption(
            "**Supported:** audio (mp3, wav, m4a, flac, ogg, aac) and "
            "video (mp4, webm, mov, mkv, avi, wmv, m4v, mpeg). "
            "For video, the audio track is transcribed."
        )

    return engine, language, whisper_model, api_key, speakers_expected, generate_summary, smart_paragraphs


def main():
    _init()

    (engine, language, whisper_model, api_key,
     speakers_expected, generate_summary, smart_paragraphs) = render_sidebar()

    st.markdown('<div class="main-title">🎙️ Audio Transcription</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="subtitle">Upload audio files · choose engine · tag speakers · export</div>',
        unsafe_allow_html=True,
    )

    # ── 1. Upload
    st.markdown('<div class="step-header">1 · Upload Audio Files</div>', unsafe_allow_html=True)
    uploaded_files = st.file_uploader(
        "Upload",
        type=UPLOAD_TYPES,
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    if uploaded_files:
        names = ", ".join(f.name for f in uploaded_files)
        st.caption(f"{len(uploaded_files)} file(s) ready: {names}")

    # ── 2. Transcribe
    st.markdown('<div class="step-header">2 · Transcribe</div>', unsafe_allow_html=True)

    ready = bool(uploaded_files) and (
        engine == "Whisper (Free, Local)" or bool(api_key)
    )

    if st.button("🎙️ Transcribe All Files", disabled=not ready, type="primary"):
        st.session_state.transcriptions = []
        st.session_state.speaker_map = {}

        progress = st.progress(0)
        status = st.empty()

        for i, f in enumerate(uploaded_files):
            status.info(f"Transcribing **{f.name}** ({i + 1}/{len(uploaded_files)}) …")
            try:
                result = _transcribe(
                    audio_bytes=f.read(),
                    filename=f.name,
                    engine=engine,
                    language=language,
                    whisper_model=whisper_model,
                    api_key=api_key,
                    speakers_expected=speakers_expected,
                    generate_summary=generate_summary,
                    smart_paragraphs=smart_paragraphs,
                )
            except Exception as exc:
                result = {
                    "filename": f.name,
                    "language": language,
                    "engine": engine,
                    "segments": [],
                    "full_text": "",
                    "summary": None,
                    "error": str(exc),
                }
            st.session_state.transcriptions.append(result)
            progress.progress((i + 1) / len(uploaded_files))

        status.success(f"✅ Done — {len(uploaded_files)} file(s) transcribed.")
        speakers = _all_speakers(st.session_state.transcriptions)
        st.session_state.speaker_map = {s: s for s in speakers}
        time.sleep(0.4)
        st.rerun()

    if not uploaded_files:
        st.caption("Upload files above to get started.")
    elif engine == "AssemblyAI (Commercial)" and not api_key:
        st.warning("Enter your AssemblyAI API key in the sidebar.")

    # ── 3. Review
    if not st.session_state.transcriptions:
        return

    st.markdown("---")
    st.markdown('<div class="step-header">3 · Review Transcriptions</div>', unsafe_allow_html=True)

    displayed = _apply_map(st.session_state.transcriptions, st.session_state.speaker_map)

    for t in displayed:
        icon = "✅" if not t.get("error") else "❌"
        with st.expander(f"{icon} {t['filename']}", expanded=True):
            if t.get("error"):
                st.error(t["error"])
                continue
            st.caption(
                f"Engine: {t['engine']} · Language: {t['language']} · {len(t['segments'])} segment(s)"
            )
            if t.get("summary"):
                st.markdown(
                    f'<div class="summary-box">✨ <strong>AI Summary</strong><br>{t["summary"]}</div>',
                    unsafe_allow_html=True,
                )
            for seg in t["segments"]:
                ts = f"{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
                st.markdown(
                    f'<div class="segment-card">'
                    f'<span class="speaker-label">{seg["speaker"]}</span>'
                    f'<span class="timestamp">{ts}</span>'
                    f'<div class="seg-text">{seg["text"]}</div>'
                    f"</div>",
                    unsafe_allow_html=True,
                )

    # ── 4. Speaker names
    st.markdown("---")
    st.markdown('<div class="step-header">4 · Tag Speaker Names</div>', unsafe_allow_html=True)

    speakers = _all_speakers(st.session_state.transcriptions)
    if not speakers:
        st.caption("No speakers detected.")
    else:
        if engine == "Whisper (Free, Local)" and speakers == ["Speaker 1"]:
            st.info(
                "Whisper produced a single speaker track. "
                "Use AssemblyAI for automatic multi-speaker detection.",
                icon="ℹ️",
            )

        st.caption("Map detected speaker IDs to real names, then click Apply.")
        ncols = min(len(speakers), 4)
        cols = st.columns(ncols)
        new_map: Dict[str, str] = {}
        for i, sp in enumerate(speakers):
            with cols[i % ncols]:
                name = st.text_input(
                    sp,
                    value=st.session_state.speaker_map.get(sp, sp),
                    key=f"spk_{sp}",
                )
                new_map[sp] = name.strip() or sp

        if st.button("✅ Apply Speaker Names"):
            st.session_state.speaker_map = new_map
            st.rerun()

    # ── 5. Export
    st.markdown("---")
    st.markdown('<div class="step-header">5 · Export</div>', unsafe_allow_html=True)

    fmt = st.radio(
        "Format",
        list(FORMATS.keys()),
        format_func=lambda k: FORMATS[k],
        horizontal=True,
        label_visibility="collapsed",
    )

    valid = [t for t in st.session_state.transcriptions if not t.get("error")]
    if not valid:
        st.warning("No successfully transcribed files to export.")
        return

    cols = st.columns(min(len(valid), 4))
    for i, t in enumerate(valid):
        with cols[i % min(len(valid), 4)]:
            stem = Path(t["filename"]).stem
            try:
                data = _export(t, st.session_state.speaker_map, fmt)
                st.download_button(
                    label=f"⬇️ Download {t['filename']}",
                    data=data,
                    file_name=f"{stem}.{fmt}",
                    mime=MIME[fmt],
                    key=f"dl_{i}_{fmt}",
                )
            except Exception as exc:
                st.error(f"{t['filename']}: {exc}")

            if t.get("summary"):
                try:
                    sdata = _export_summary(t, fmt)
                    st.download_button(
                        label="⬇️ Download Summary",
                        data=sdata,
                        file_name=f"{stem}_summary.{fmt}",
                        mime=MIME[fmt],
                        key=f"dlsum_{i}_{fmt}",
                    )
                except Exception as exc:
                    st.error(f"{t['filename']} summary: {exc}")

    if len(valid) > 1:
        st.markdown(" ")
        try:
            zip_data = _build_zip(valid, st.session_state.speaker_map, fmt)
            st.download_button(
                label="⬇️ Download All as ZIP",
                data=zip_data,
                file_name="transcriptions.zip",
                mime="application/zip",
                type="primary",
            )
        except Exception as exc:
            st.error(f"ZIP error: {exc}")

    st.markdown(" ")
    st.caption("Get everything in one archive — all formats (txt, docx, pdf, md) plus summaries where available.")
    try:
        full_zip = _build_full_zip(valid, st.session_state.speaker_map)
        st.download_button(
            label="📦 Download Everything (all formats + summaries)",
            data=full_zip,
            file_name="transcriptions_all_formats.zip",
            mime="application/zip",
            type="primary",
            key="dl_full_zip",
        )
    except Exception as exc:
        st.error(f"Full ZIP error: {exc}")


if __name__ == "__main__":
    main()
