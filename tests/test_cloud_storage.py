"""Tests for the signed upload policy and the hosted-mode app wiring.

Run with:  python -m pytest tests
"""

import base64
import binascii
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cloud_storage  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(data: bytes) -> bytes:
    return KEY.sign(data, padding.PKCS1v15(), hashes.SHA256())


def test_policy_is_signed_and_scoped_to_session_prefix():
    now = dt.datetime(2026, 11, 22, 14, 30, 0, tzinfo=dt.timezone.utc)
    pol = cloud_storage.build_post_policy(
        bucket="my-bucket",
        prefix="uploads/abc/",
        max_bytes=1024,
        service_account_email="app@proj.iam.gserviceaccount.com",
        sign=_sign,
        now=now,
    )
    f = pol["fields"]
    assert pol["url"] == "https://storage.googleapis.com/my-bucket/"
    assert f["x-goog-algorithm"] == "GOOG4-RSA-SHA256"
    assert f["x-goog-date"] == "20261122T143000Z"
    assert f["x-goog-credential"] == (
        "app@proj.iam.gserviceaccount.com/20261122/auto/storage/goog4_request"
    )

    # Signature verifies over the base64 policy string.
    KEY.public_key().verify(
        binascii.unhexlify(f["x-goog-signature"]),
        f["policy"].encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    doc = json.loads(base64.b64decode(f["policy"]))
    assert doc["expiration"] == "2026-11-22T16:30:00Z"
    conds = doc["conditions"]
    assert {"bucket": "my-bucket"} in conds
    assert ["starts-with", "$key", "uploads/abc/"] in conds
    assert ["content-length-range", 1, 1024] in conds
    # Every form field sent by the browser (except signature/policy/key/file)
    # must appear as an exact-match condition, or GCS rejects the upload.
    for k, v in f.items():
        if k not in ("policy", "x-goog-signature"):
            assert {k: v} in conds, k


def test_display_name_strips_timestamp():
    p = "uploads/abc/"
    assert cloud_storage.display_name(p + "1732280000000-interview 1.mp3", p) == "interview 1.mp3"
    assert cloud_storage.display_name(p + "plain.wav", p) == "plain.wav"


def test_enabled_follows_env(monkeypatch):
    monkeypatch.delenv("GCS_UPLOAD_BUCKET", raising=False)
    assert not cloud_storage.enabled()
    monkeypatch.setenv("GCS_UPLOAD_BUCKET", " b ")
    assert cloud_storage.enabled() and cloud_storage.bucket_name() == "b"


# ── App-level checks with Streamlit's test runner ───────────────────────────

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(ROOT / "app.py")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("APP_PASSWORD", "GCS_UPLOAD_BUCKET", "ASSEMBLYAI_API_KEY", "WHISPER_MODELS_ALLOWED"):
        monkeypatch.delenv(k, raising=False)
    # load_dotenv() in app.py must not pull in a developer's real .env
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)


def test_local_mode_shows_normal_uploader():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert len(at.get("file_uploader")) == 1


def test_password_gate(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret\n")  # trailing newline is stripped
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert len(at.get("file_uploader")) == 0
    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert at.error and "Wrong password" in at.error[0].value
    at.text_input[0].input("s3cret")
    at.button[0].click().run()
    assert not at.exception
    assert len(at.get("file_uploader")) == 1


def test_server_key_is_not_sent_to_browser(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "super-secret-key")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.selectbox[0].select("AssemblyAI (Commercial)").run()
    assert not at.exception
    assert all(t.value != "super-secret-key" for t in at.text_input)
    assert any("server settings" in c.value for c in at.sidebar.caption)


def test_whisper_models_can_be_limited(monkeypatch):
    monkeypatch.setenv("WHISPER_MODELS_ALLOWED", "tiny, base,small")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert at.sidebar.selectbox[2].options == ["tiny", "base", "small"]
    assert at.sidebar.selectbox[2].value == "base"


def test_cloud_mode_lists_bucket_files(monkeypatch):
    monkeypatch.setenv("GCS_UPLOAD_BUCKET", "my-bucket")

    class Blob:
        def __init__(self, name, size):
            self.name, self.size = name, size

    class FakeClient:
        def list_blobs(self, bucket, prefix):
            return [Blob(prefix + "1732280000000-talk.mp3", 5 * 1048576)]

    monkeypatch.setattr(cloud_storage, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        cloud_storage, "_default_signer",
        lambda: ("app@proj.iam.gserviceaccount.com", _sign),
    )
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert len(at.get("file_uploader")) == 0
    assert any("talk.mp3" in c.value for c in at.caption)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_video_upload_is_converted_to_compact_audio(monkeypatch, tmp_path):
    import subprocess

    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-shortest", str(video)],
        check=True,
    )

    class Blob:
        def download_to_filename(self, dest):
            Path(dest).write_bytes(video.read_bytes())

    class Bucket:
        def blob(self, name):
            return Blob()

    class FakeClient:
        def bucket(self, name):
            return Bucket()

    monkeypatch.setenv("GCS_UPLOAD_BUCKET", "b")
    monkeypatch.setattr(cloud_storage, "_client", lambda: FakeClient())
    up = cloud_storage.StoredUpload(name="clip.mp4", size=video.stat().st_size, blob_name="uploads/x/1-clip.mp4")
    data = up.read()
    assert data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # MP3
    assert len(data) < video.stat().st_size


def test_public_mode_whisper_off_and_users_paste_key(monkeypatch):
    monkeypatch.setenv("GCS_UPLOAD_BUCKET", "my-bucket")
    monkeypatch.setenv("WHISPER_ENABLED", "false")

    class FakeClient:
        def list_blobs(self, bucket, prefix):
            return []

    monkeypatch.setattr(cloud_storage, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        cloud_storage, "_default_signer",
        lambda: ("app@proj.iam.gserviceaccount.com", _sign),
    )
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert at.sidebar.selectbox[0].options == ["AssemblyAI (Commercial)"]
    key_box = [t for t in at.sidebar.text_input if "AssemblyAI API key" in t.label]
    assert len(key_box) == 1 and key_box[0].value == ""
    assert any("never saved" in c.value for c in at.sidebar.caption)
    key_box[0].input("  user-key-123  ").run()
    assert not at.exception
    assert not any("Paste your AssemblyAI API key" in w.value for w in at.warning)
