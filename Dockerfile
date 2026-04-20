# Image Compression Recommender — production image.
#
# Critical: the app shells out to `cwebp` and `avifenc`, which are system
# binaries (not Python packages). They MUST be present in the image. Generic
# Python base images do not include them, so this Dockerfile installs them
# from apt before running the app.

FROM python:3.11-slim

# System dependencies:
#   webp         -> cwebp
#   libavif-bin  -> avifenc
#   libjpeg/zlib/libpng -> Pillow needs these to decode common inputs
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      webp \
      libavif-bin \
      libjpeg62-turbo \
      zlib1g \
      libpng16-16 \
      libtiff6 \
      libwebp7 \
    && rm -rf /var/lib/apt/lists/*

# Verify encoders are on PATH at build time — fail fast if something changed.
RUN cwebp -version && avifenc --version

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# App code.
COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts

# Build-time safety nets — fail the BUILD (not the runtime) if either
# of these regresses. These would have caught the merge-marker incident
# that caused the Render deploy outage.
RUN bash scripts/check_no_conflict_markers.sh
RUN python -m compileall -q backend

# Data dir for uploads/variants/results. On platforms with ephemeral disks
# (Render free tier, Fly.io without volumes) this is wiped on restart — that
# is acceptable for a test-user build.
RUN mkdir -p /app/data/images /app/data/variants /app/data/results /app/data/exports

# Most PaaS providers set $PORT. Default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

# Production server: single worker is fine for a prototype and avoids
# filesystem race conditions on the shared data/ dir.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
