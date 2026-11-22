# Container image for Google Cloud Run.
# Build and deploy with:  gcloud run deploy --source .   (see deploy/README.md)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Whisper looks for its models in $XDG_CACHE_HOME/whisper
    XDG_CACHE_HOME=/opt/cache \
    # Larger models don't fit the default 4 GiB instance (see deploy/README.md)
    WHISPER_MODELS_ALLOWED="tiny,base,small"

# ffmpeg: needed by Whisper and for extracting audio from video uploads
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch first: the default Linux wheel bundles GPU libraries
# (several GB) that Cloud Run's CPU instances can't use.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the allowed Whisper models into the image so they aren't downloaded on
# every cold start.
RUN python -c "import whisper; [whisper._download(whisper._MODELS[m], '/opt/cache/whisper', False) for m in ('tiny', 'base', 'small')]"

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser /app \
    && chmod -R a+rX /opt/cache
USER appuser

# Cloud Run sends traffic to $PORT (8080 by default).
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true --server.maxUploadSize=32 --browser.gatherUsageStats=false"]
