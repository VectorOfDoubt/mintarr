FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git ffmpeg flac libsndfile1 ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Radexito fork — den eneste som leverer HiRes 24-bit/88+ kHz fra TIDAL i 2026.
# Pinnet til SHA 2026-05-22 så rebuilds er reproducerbare. Bump manuelt ved behov
# og smoke-test mot TIDAL (auth + HiRes-download) før du committer.
RUN pip install --no-cache-dir \
    "git+https://github.com/Radexito/tidal-dl-ng-For-DJ.git@87ec210dfeeef23441b7c99a16123a25ec63f207" \
    flask==3.1.3 \
    gunicorn==26.0.0 \
    requests==2.32.5

# tidal-dl-ng config-folder
ENV TIDAL_DL_NG_CONFIG=/config/tidal_dl_ng

RUN mkdir -p /downloads /output /config/tidal_dl_ng
WORKDIR /app
# Copy entire app tree so subpackages (adapters/) are included.
# .dockerignore excludes __pycache__ etc.
COPY app/ /app/

EXPOSE 8000

# -w 1: single worker fordi _jobs er in-memory shared state (workers deler ikke RAM)
# Flere workers gir race conditions hvor /jobs/queue/history returnerer ulik data
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "1200", "--access-logfile", "-", "--access-logformat", "%(h)s %(l)s %(u)s %(t)s \"%(m)s %(U)s %(H)s\" %(s)s %(b)s \"%(f)s\" \"%(a)s\"", "server:app"]
