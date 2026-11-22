"""Direct browser -> Google Cloud Storage uploads for the hosted (Cloud Run) app.

Why this exists
---------------
Cloud Run rejects HTTP/1 request bodies larger than 32 MiB, and Streamlit's
built-in ``st.file_uploader`` sends each file in a single request. An hour of
interview audio is easily 50+ MB, and video is far larger.

When the ``GCS_UPLOAD_BUCKET`` environment variable is set, the app therefore
skips ``st.file_uploader``. Instead it hands the browser a short-lived *signed
upload policy* that lets it POST files straight into a private bucket (no size
limit from Cloud Run). The app then lists the files under this browser
session's folder, reads them from the bucket, and deletes them once they have
been transcribed. A bucket lifecycle rule removes anything left behind after
one day (see ``deploy/lifecycle.json``).

When ``GCS_UPLOAD_BUCKET`` is not set (e.g. running locally), none of this is
used and the normal uploader appears.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import streamlit as st

BUCKET_ENV = "GCS_UPLOAD_BUCKET"
# Largest file the signed policy accepts. The whole file is copied into the
# container's in-memory disk while it is converted, so keep this well below
# the Cloud Run memory setting.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))

POLICY_TTL = dt.timedelta(hours=2)
# Re-sign a little before expiry so an upload never starts on a stale policy.
POLICY_REFRESH_MARGIN = dt.timedelta(minutes=20)

# Video files and very large audio files are converted to compact mono MP3
# before transcription. Speech quality is unaffected, and it keeps memory use
# and upload time to AssemblyAI low.
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".mpeg", ".mpg"}
CONVERT_ABOVE_BYTES = 100 * 1024 * 1024


def enabled() -> bool:
    return bool(os.environ.get(BUCKET_ENV, "").strip())


def bucket_name() -> str:
    return os.environ[BUCKET_ENV].strip()


# ── Signing ──────────────────────────────────────────────────────────────────

def _default_signer() -> tuple[str, Callable[[bytes], bytes]]:
    """Return (service_account_email, sign_fn) for the app's credentials.

    On Cloud Run the runtime credentials carry no private key, so signing goes
    through the IAM Credentials API (the service account needs the
    ``roles/iam.serviceAccountTokenCreator`` role on itself). With a local
    service-account key file, the key signs directly.
    """
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = Request()
    if getattr(creds, "signer", None) is not None and hasattr(creds, "sign_bytes"):
        return creds.service_account_email, creds.sign_bytes

    creds.refresh(request)
    email = getattr(creds, "service_account_email", None)
    if not email or email == "default":
        raise RuntimeError(
            "Could not determine the service account email needed to sign uploads."
        )
    signer = iam.Signer(request, creds, email)
    return email, signer.sign


def build_post_policy(
    bucket: str,
    prefix: str,
    max_bytes: int,
    service_account_email: str,
    sign: Callable[[bytes], bytes],
    now: Optional[dt.datetime] = None,
    ttl: dt.timedelta = POLICY_TTL,
) -> Dict:
    """Build a V4 signed POST policy that allows uploads under ``prefix`` only.

    google-cloud-storage's helper pins one exact object name, but the browser
    names each file itself, so the policy is assembled here with a
    ``starts-with`` condition on the key. Format reference:
    https://cloud.google.com/storage/docs/authentication/signatures#policy-document
    """
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    datestamp = now.strftime("%Y%m%d")
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    credential = f"{service_account_email}/{datestamp}/auto/storage/goog4_request"
    expires_at = now + ttl

    fields = {
        "success_action_status": "201",
        "x-goog-algorithm": "GOOG4-RSA-SHA256",
        "x-goog-credential": credential,
        "x-goog-date": timestamp,
    }
    conditions = [
        {"bucket": bucket},
        ["starts-with", "$key", prefix],
        ["content-length-range", 1, int(max_bytes)],
        *({k: v} for k, v in fields.items()),
    ]
    policy = json.dumps(
        {"conditions": conditions, "expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")},
        separators=(",", ":"),
    )
    policy_b64 = base64.b64encode(policy.encode("utf-8"))
    signature = binascii.hexlify(sign(policy_b64)).decode("ascii")

    return {
        "url": f"https://storage.googleapis.com/{bucket}/",
        "prefix": prefix,
        "expires_at": expires_at.isoformat(),
        "fields": {**fields, "policy": policy_b64.decode("ascii"), "x-goog-signature": signature},
    }


# ── Bucket access ────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _client():
    from google.cloud import storage

    return storage.Client()


def _session_prefix() -> str:
    """Each browser session gets its own folder, so users never see each
    other's files."""
    if "gcs_prefix" not in st.session_state:
        st.session_state.gcs_prefix = f"uploads/{uuid.uuid4().hex}/"
    return st.session_state.gcs_prefix


def _current_policy() -> Dict:
    pol = st.session_state.get("gcs_policy")
    now = dt.datetime.now(dt.timezone.utc)
    if pol is None or dt.datetime.fromisoformat(pol["expires_at"]) - now < POLICY_REFRESH_MARGIN:
        email, sign = _default_signer()
        pol = build_post_policy(
            bucket=bucket_name(),
            prefix=_session_prefix(),
            max_bytes=MAX_UPLOAD_MB * 1024 * 1024,
            service_account_email=email,
            sign=sign,
        )
        st.session_state.gcs_policy = pol
    return pol


def display_name(blob_name: str, prefix: str) -> str:
    """Object keys look like ``<prefix><millis>-<file name>``."""
    rest = blob_name[len(prefix):]
    stamp, sep, name = rest.partition("-")
    return name if sep and stamp.isdigit() else rest


@dataclass
class StoredUpload:
    """Quacks like Streamlit's UploadedFile for the parts app.py uses."""

    name: str
    size: int
    blob_name: str

    def read(self) -> bytes:
        bucket = _client().bucket(bucket_name())
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / ("source" + Path(self.name).suffix.lower())
            bucket.blob(self.blob_name).download_to_filename(str(src))
            if src.suffix in VIDEO_EXTS or self.size > CONVERT_ABOVE_BYTES:
                converted = _to_compact_mp3(src)
                if converted is not None:
                    return converted
            return src.read_bytes()

    def delete(self) -> None:
        try:
            _client().bucket(bucket_name()).blob(self.blob_name).delete()
        except Exception:
            pass  # the lifecycle rule cleans up anything missed


def _to_compact_mp3(src: Path) -> Optional[bytes]:
    """Extract the audio track as mono 16 kHz 64 kbps MP3 (~28 MB per hour).
    Returns None if ffmpeg fails, so the caller can fall back to the original."""
    out = src.with_name("converted.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)],
            check=True,
            capture_output=True,
        )
        src.unlink(missing_ok=True)  # free the in-memory disk early
        return out.read_bytes()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def list_uploads() -> List[StoredUpload]:
    prefix = _session_prefix()
    blobs = _client().list_blobs(bucket_name(), prefix=prefix)
    items = [
        StoredUpload(name=display_name(b.name, prefix), size=b.size or 0, blob_name=b.name)
        for b in blobs
        if not b.name.endswith("/")
    ]
    return sorted(items, key=lambda u: u.blob_name)


# ── Browser upload widget ────────────────────────────────────────────────────

_HTML = """
<div class="gcs-up">
  <label class="drop">
    <input type="file" multiple>
    <strong>Choose audio or video files</strong>
    <span class="hint"></span>
  </label>
  <div class="rows"></div>
</div>
"""

_CSS = """
.gcs-up { font-family: var(--st-font, sans-serif); }
.drop {
  display: block; cursor: pointer; padding: 1.4rem 1rem; text-align: center;
  border: 1px dashed var(--st-border-color, #bbb); border-radius: 0.5rem;
  background: var(--st-secondary-background-color, #f6f7f9);
}
.drop input { display: none; }
.drop .hint { display: block; margin-top: 0.3rem; font-size: 0.85rem; opacity: 0.7; }
.row { margin-top: 0.6rem; font-size: 0.9rem; }
.row .bar { height: 6px; border-radius: 3px; background: var(--st-border-color, #ddd); overflow: hidden; margin-top: 0.25rem; }
.row .bar > div { height: 100%; width: 0; background: var(--st-primary-color, #0066cc); transition: width 0.2s; }
.row.err { color: #c62828; }
.row.ok .label::after { content: "  ✓"; }
"""

_JS = """
export default function(component) {
  const { data, parentElement, setTriggerValue } = component;
  const input = parentElement.querySelector('input[type=file]');
  const rows = parentElement.querySelector('.rows');
  parentElement.querySelector('.hint').textContent =
    'Uploaded securely to cloud storage. Up to ' + data.max_mb + ' MB per file.';
  input.accept = data.accept;

  const safe = (n) => n.replace(/[^A-Za-z0-9._ \\-()]/g, '_').slice(0, 180);
  const mb = (b) => (b / 1048576).toFixed(1) + ' MB';

  function uploadOne(file, index) {
    return new Promise((resolve) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<span class="label"></span><div class="bar"><div></div></div>';
      row.querySelector('.label').textContent = file.name + ' (' + mb(file.size) + ')';
      rows.appendChild(row);
      const bar = row.querySelector('.bar > div');
      const fail = (msg) => {
        row.classList.add('err');
        row.querySelector('.label').textContent = file.name + ': ' + msg;
        resolve(false);
      };

      if (file.size > data.max_mb * 1048576) return fail('larger than ' + data.max_mb + ' MB');
      if (Date.parse(data.expires_at) < Date.now()) return fail('upload link expired, reload the page');

      const form = new FormData();
      for (const [k, v] of Object.entries(data.fields)) form.append(k, v);
      form.append('key', data.prefix + (Date.now() + index) + '-' + safe(file.name));
      form.append('file', file);  // must be the last field

      const xhr = new XMLHttpRequest();
      xhr.open('POST', data.url);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total) + '%';
      };
      xhr.onload = () => {
        if (xhr.status === 201 || xhr.status === 204) {
          bar.style.width = '100%';
          row.classList.add('ok');
          resolve(true);
        } else {
          fail('upload failed (HTTP ' + xhr.status + ')');
        }
      };
      xhr.onerror = () => fail('network error');
      xhr.send(form);
    });
  }

  input.onchange = async () => {
    const files = Array.from(input.files || []);
    input.value = '';
    if (!files.length) return;
    let ok = 0;
    for (let i = 0; i < files.length; i++) {
      if (await uploadOne(files[i], i)) ok++;
    }
    // Tell Python to re-run and pick up the new files from the bucket.
    setTriggerValue('uploaded', { ok: ok, total: files.length, at: Date.now() });
  };

  return () => { input.onchange = null; };
}
"""


@st.cache_resource(show_spinner=False)
def _component():
    import streamlit.components.v2 as components_v2

    return components_v2.component("gcs_direct_upload", html=_HTML, css=_CSS, js=_JS)


def render_uploader(accept_exts: List[str]) -> List[StoredUpload]:
    """Show the upload widget and return the files already in the bucket."""
    try:
        pol = _current_policy()
    except Exception as exc:  # misconfigured permissions, etc.
        st.error(f"Cloud upload is not available: {exc}")
        return []

    _component()(
        key="gcs_uploader",
        data={
            "url": pol["url"],
            "fields": pol["fields"],
            "prefix": pol["prefix"],
            "expires_at": pol["expires_at"],
            "max_mb": MAX_UPLOAD_MB,
            "accept": ",".join("." + e for e in accept_exts),
        },
        on_uploaded_change=lambda: None,
    )

    try:
        uploads = list_uploads()
    except Exception as exc:
        st.error(f"Could not list uploaded files: {exc}")
        return []

    if uploads:
        total_mb = sum(u.size for u in uploads) / 1048576
        c1, c2 = st.columns([4, 1])
        with c1:
            st.caption(f"{total_mb:.1f} MB waiting in cloud storage (deleted after transcription).")
        with c2:
            if st.button("🗑️ Clear files", key="gcs_clear"):
                for u in uploads:
                    u.delete()
                st.rerun()
    return uploads
