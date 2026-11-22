import hmac
import io
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import streamlit as st

load_dotenv()  # reads .env if present

import cloud_storage  # noqa: E402  (direct-to-bucket uploads when hosted)

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
    "srt": "Subtitles (.srt)",
}
MIME = {
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "srt": "application/x-subrip",
}
# Formats that support a standalone summary export. SRT is a subtitle format
# with no notion of a summary cue, so it is excluded.
SUMMARY_FORMATS = {"txt", "docx", "pdf", "md"}
# Accepted upload types. Video containers are supported too — both Whisper
# (via ffmpeg) and AssemblyAI extract the audio track automatically.
UPLOAD_TYPES = [
    "mp3", "wav", "m4a", "flac", "ogg", "aac",
    "mp4", "webm", "mov", "mkv", "avi", "wmv", "m4v", "mpeg", "mpg",
]


def _init():
    if "transcriptions" not in st.session_state:
        st.session_state.transcriptions = []


def _env(name: str) -> str:
    """Environment value with stray whitespace/newlines removed (secrets
    created from a file often end with a newline)."""
    return os.environ.get(name, "").strip()


def _allowed_whisper_models() -> List[str]:
    """WHISPER_MODELS_ALLOWED (e.g. "tiny,base,small") limits the model list,
    so a hosted instance isn't asked to load a model larger than its memory."""
    raw = _env("WHISPER_MODELS_ALLOWED")
    if not raw:
        return WHISPER_MODELS
    wanted = {m.strip() for m in raw.replace(" ", ",").split(",") if m.strip()}
    return [m for m in WHISPER_MODELS if m in wanted] or WHISPER_MODELS


def _password_gate() -> bool:
    """If APP_PASSWORD is set, ask for it once per browser session."""
    password = _env("APP_PASSWORD")
    if not password or st.session_state.get("authenticated"):
        return True

    st.markdown('<div class="main-title">🎙️ Audio Transcription</div>', unsafe_allow_html=True)
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state.authenticated = True
            st.rerun()
        time.sleep(1.5)  # slow down guessing
        st.error("Wrong password.")
    return False


def _fmt_time(seconds: float) -> str:
    if seconds is None:
        return "0:00:00"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _track_speakers(transcription: Dict) -> List[str]:
    """Ordered, unique speaker IDs within a single transcription (track)."""
    seen: List[str] = []
    for seg in transcription.get("segments", []):
        sp = seg.get("speaker", "Speaker 1")
        if sp not in seen:
            seen.append(sp)
    return seen


def _apply_map(transcriptions: List[Dict]) -> List[Dict]:
    """Return copies with each track's own speaker_map applied to its segments."""
    out = []
    for t in transcriptions:
        smap = t.get("speaker_map", {})
        tc = dict(t)
        tc["segments"] = [
            {**s, "speaker": smap.get(s["speaker"], s["speaker"])}
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


def _export(transcription: Dict, fmt: str) -> bytes:
    from exporters.export_utils import (
        export_txt, export_docx, export_pdf, export_md, export_srt,
    )
    fn = {
        "txt": export_txt,
        "docx": export_docx,
        "pdf": export_pdf,
        "md": export_md,
        "srt": export_srt,
    }[fmt]
    return fn(transcription, transcription.get("speaker_map", {}))


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


def _build_zip(transcriptions: List[Dict], fmt: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in transcriptions:
            if t.get("error"):
                continue
            data = _export(t, fmt)
            zf.writestr(f"{Path(t['filename']).stem}.{fmt}", data)
    buf.seek(0)
    return buf.read()


def _build_full_zip(transcriptions: List[Dict]) -> bytes:
    """ZIP with every format for every file, plus a summary file per format
    when a summary is available."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in transcriptions:
            if t.get("error"):
                continue
            stem = Path(t["filename"]).stem
            for fmt in FORMATS:
                zf.writestr(f"{stem}/{stem}.{fmt}", _export(t, fmt))
                if t.get("summary") and fmt in SUMMARY_FORMATS:
                    zf.writestr(f"{stem}/{stem}_summary.{fmt}", _export_summary(t, fmt))
    buf.seek(0)
    return buf.read()


def render_sidebar():
    env_key = _env("ASSEMBLYAI_API_KEY")
    hosted = cloud_storage.enabled()

    with st.sidebar:
        st.header("⚙️ Settings")

        whisper_where = (
            "runs on this app's own server (no third-party service)"
            if hosted else "runs entirely on your machine"
        )
        engine = st.selectbox(
            "Engine",
            ["Whisper (Free, Local)", "AssemblyAI (Commercial)"],
            help=(
                f"**Whisper** {whisper_where} — free, no API key needed. "
                "**AssemblyAI** uses the EU region (Universal-3.5 Pro model) with automatic speaker diarization."
            ),
        )

        language = st.selectbox("Language", LANGUAGES)

        whisper_model = None
        api_key = None
        generate_summary = False
        smart_paragraphs = False

        if engine == "Whisper (Free, Local)":
            st.markdown("---")
            st.subheader("Whisper")
            models = _allowed_whisper_models()
            whisper_model = st.selectbox(
                "Model size", models, index=models.index("base") if "base" in models else 0,
            )
            st.caption(WHISPER_MODEL_INFO[whisper_model])
            st.info(
                "Speaker diarization is not available with local Whisper. "
                "Switch to AssemblyAI for automatic multi-speaker detection.",
                icon="ℹ️",
            )
        else:
            st.markdown("---")
            st.subheader("AssemblyAI")
            # A configured key is used server-side and never sent to the
            # browser (a pre-filled password box would expose it in the page).
            use_own_key = False
            if env_key:
                st.caption("🔒 Using the AssemblyAI key from your .env / server settings.")
                use_own_key = st.checkbox("Use a different key", value=False)
            if not env_key or use_own_key:
                api_key = st.text_input(
                    "API Key",
                    type="password",
                    placeholder="xxxxxxxxxxxxxxxxxxxxxxxx",
                    help="Set ASSEMBLYAI_API_KEY in .env to skip this. Get a key at assemblyai.com.",
                )
                if not api_key:
                    st.warning("Enter your AssemblyAI API key to continue.")
            else:
                api_key = env_key

            generate_summary = st.checkbox(
                "Generate AI summary after transcription",
                value=False,
                help="Uses AssemblyAI LLM Gateway (EU) to produce a 3-5 paragraph summary of each transcript.",
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

    return engine, language, whisper_model, api_key, generate_summary, smart_paragraphs


def main():
    _init()
    if not _password_gate():
        return

    (engine, language, whisper_model, api_key,
     generate_summary, smart_paragraphs) = render_sidebar()

    st.markdown('<div class="main-title">🎙️ Audio Transcription</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="subtitle">Upload audio files · choose engine · tag speakers · export</div>',
        unsafe_allow_html=True,
    )

    # ── 1. Upload
    st.markdown('<div class="step-header">1 · Upload Audio Files</div>', unsafe_allow_html=True)
    if cloud_storage.enabled():
        # Hosted: the browser uploads straight to Cloud Storage (no 32 MB cap).
        uploaded_files = cloud_storage.render_uploader(UPLOAD_TYPES)
    else:
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

    # Per-file expected speaker count (AssemblyAI only). Each track can have a
    # different number of speakers — 0 means auto-detect for that file.
    per_file_speakers: Dict[int, Optional[int]] = {}
    if uploaded_files and engine == "AssemblyAI (Commercial)":
        st.caption(
            "Expected speakers per file (0 = auto-detect). Set each track "
            "independently — they can have different speaker counts."
        )
        ncols = min(len(uploaded_files), 4)
        scols = st.columns(ncols)
        for i, f in enumerate(uploaded_files):
            with scols[i % ncols]:
                n = st.number_input(
                    f.name,
                    min_value=0,
                    max_value=20,
                    value=0,
                    step=1,
                    key=f"spkcount_{i}",
                )
                per_file_speakers[i] = int(n) if n > 0 else None

    ready = bool(uploaded_files) and (
        engine == "Whisper (Free, Local)" or bool(api_key)
    )

    if st.button("🎙️ Transcribe All Files", disabled=not ready, type="primary"):
        st.session_state.transcriptions = []

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
                    speakers_expected=per_file_speakers.get(i),
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
            # Each track gets its own speaker map so labels never bleed across files.
            result["speaker_map"] = {s: s for s in _track_speakers(result)}
            st.session_state.transcriptions.append(result)
            # Hosted: remove the recording from cloud storage once it's done.
            # Failed files stay so they can be retried (the bucket's lifecycle
            # rule deletes leftovers after a day).
            if not result.get("error") and hasattr(f, "delete"):
                f.delete()
            progress.progress((i + 1) / len(uploaded_files))

        status.success(f"✅ Done — {len(uploaded_files)} file(s) transcribed.")
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

    displayed = _apply_map(st.session_state.transcriptions)

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

    taggable = [
        (i, t) for i, t in enumerate(st.session_state.transcriptions)
        if not t.get("error") and _track_speakers(t)
    ]
    if not taggable:
        st.caption("No speakers detected.")
    else:
        st.caption("Rename speakers separately for each file, then click Apply.")
        apply = st.button("✅ Apply Speaker Names")
        pending: Dict[int, Dict[str, str]] = {}
        for i, t in taggable:
            speakers = _track_speakers(t)
            st.markdown(f"**🎧 {t['filename']}**")
            if engine == "Whisper (Free, Local)" and speakers == ["Speaker 1"]:
                st.info(
                    "Whisper produced a single speaker track. "
                    "Use AssemblyAI for automatic multi-speaker detection.",
                    icon="ℹ️",
                )
            smap = t.get("speaker_map", {})
            ncols = min(len(speakers), 4)
            cols = st.columns(ncols)
            per: Dict[str, str] = {}
            for j, sp in enumerate(speakers):
                with cols[j % ncols]:
                    name = st.text_input(
                        sp,
                        value=smap.get(sp, sp),
                        key=f"spk_{i}_{sp}",
                    )
                    per[sp] = name.strip() or sp
            pending[i] = per

        if apply:
            for idx, per_map in pending.items():
                st.session_state.transcriptions[idx]["speaker_map"] = per_map
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
                data = _export(t, fmt)
                st.download_button(
                    label=f"⬇️ Download {t['filename']}",
                    data=data,
                    file_name=f"{stem}.{fmt}",
                    mime=MIME[fmt],
                    key=f"dl_{i}_{fmt}",
                )
            except Exception as exc:
                st.error(f"{t['filename']}: {exc}")

            if t.get("summary") and fmt in SUMMARY_FORMATS:
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
            zip_data = _build_zip(valid, fmt)
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
    st.caption("Get everything in one archive — all formats (txt, docx, pdf, md, srt) plus summaries where available.")
    try:
        full_zip = _build_full_zip(valid)
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
