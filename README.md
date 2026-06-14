# 🎙️ Audio Transcription

A [Streamlit](https://streamlit.io/) web app for transcribing audio files, tagging speakers, and exporting clean transcripts. Choose between **free local Whisper** or **commercial AssemblyAI** (with automatic speaker diarization and optional AI summaries).

## Features

- **Two transcription engines**
  - **Whisper (free, local)** — runs entirely on your machine, no API key, fully private. Model sizes from `tiny` to `large`.
  - **AssemblyAI (commercial)** — EU region, Universal-3 Pro model, automatic speaker diarization.
- **Multi-file upload** — transcribe a whole batch in one run.
- **Speaker diarization** (AssemblyAI) — automatically detects who spoke when; optionally provide the expected speaker count to improve accuracy.
- **Speaker tagging** — map detected speaker IDs to real names and apply across all transcripts.
- **AI summaries** (AssemblyAI) — optional 3–5 bullet-point summary per transcript via the LLM Gateway.
- **Smart paragraph grouping** (AssemblyAI, single-speaker) — uses an LLM to regroup sentences into semantic paragraphs while preserving exact words and timestamps.
- **Export** — download as Plain Text (`.txt`), Word (`.docx`), PDF (`.pdf`), Markdown (`.md`), or SubRip subtitles (`.srt`), individually or all at once as a ZIP. AI summaries can be downloaded as separate files, and a one-click "Download Everything" archive bundles every format plus summaries.
- **Languages** — English, Spanish, Italian.

**Supported formats:**
- **Audio:** mp3, wav, m4a, flac, ogg, aac
- **Video:** mp4, webm, mov, mkv, avi, wmv, m4v, mpeg, mpg — the audio track is extracted and transcribed automatically.

## Project structure

```
transcription/
├── app.py                          # Streamlit UI (upload → transcribe → review → tag → export)
├── requirements.txt
├── .env.example
├── transcribers/
│   ├── whisper_transcriber.py      # Local Whisper backend
│   └── assemblyai_transcriber.py   # AssemblyAI backend (diarization, summary, smart paragraphs)
└── exporters/
    └── export_utils.py             # txt / docx / pdf / md / srt exporters
```

## Getting started

### 1. Install dependencies

```bash
cd transcription
pip install -r requirements.txt
```

> **Note:** Local Whisper requires [`ffmpeg`](https://ffmpeg.org/) to be installed and available on your `PATH`.
> - macOS: `brew install ffmpeg`
> - Ubuntu/Debian: `sudo apt install ffmpeg`
> - Windows: `choco install ffmpeg`

### 2. (Optional) Configure AssemblyAI

Only needed if you want to use the commercial engine. Copy the example env file and add your key:

```bash
cp .env.example .env
# then edit .env and set ASSEMBLYAI_API_KEY
```

Get a key at [assemblyai.com](https://www.assemblyai.com/). The key is loaded automatically at startup, or you can paste it into the sidebar at runtime.

### 3. Run the app

```bash
streamlit run app.py
```

The app opens in your browser. Then:

1. **Upload** one or more audio files.
2. **Choose an engine** and language in the sidebar.
3. **Transcribe** all files.
4. **Review** the results and **tag speaker names**.
5. **Export** to your preferred format.

## Engine comparison

| | Whisper (local) | AssemblyAI |
|---|---|---|
| Cost | Free | Paid (per-usage) |
| API key | Not required | Required |
| Privacy | Fully local | Audio sent to AssemblyAI (EU) |
| Speaker diarization | ❌ | ✅ |
| AI summary | ❌ | ✅ |
| Smart paragraphs | ❌ | ✅ (single-speaker) |
| Speed | Depends on model size & hardware | Cloud-processed |

## Notes

- The AI summary and smart-paragraph features use the AssemblyAI **LLM Gateway**, which is a separate product from transcription. If you see an `HTTP 401` on summaries, enable LLM Gateway and set up billing in your AssemblyAI dashboard.
- `.env` is git-ignored so your API key is never committed.

## License

Released under the [MIT License](LICENSE).
