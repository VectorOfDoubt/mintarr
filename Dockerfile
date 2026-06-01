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

# The pinned Radexito CLI still defaults stored tokens to non-PKCE OAuth,
# which makes TIDAL return AAC/HIGH even when HI_RES_LOSSLESS is configured.
# Force PKCE for stored-token loads and new browser logins until upstream fixes it.
RUN python - <<'PY'
from pathlib import Path
import inspect
import tidal_dl_ng.config as config

path = Path(inspect.getfile(config))
text = path.read_text()
text = text.replace(
    "def login_token(self, do_pkce: bool = False) -> bool:",
    "def login_token(self, do_pkce: bool = True) -> bool:",
)
text = text.replace(
    "            # Login method: Device linking\n"
    "            self.session.login_oauth_simple(fn_print)\n"
    "            # Login method: PKCE authorization (was necessary for HI_RES_LOSSLESS streaming earlier)\n"
    "            # self.session.login_pkce(fn_print)\n",
    "            # Login method: PKCE authorization (required for LOSSLESS/HI_RES streaming)\n"
    "            self.session.login_pkce(fn_print)\n",
)
path.write_text(text)

patched = path.read_text()
if "def login_token(self, do_pkce: bool = True) -> bool:" not in patched:
    raise SystemExit("failed to patch tidal-dl-ng stored-token PKCE default")
if "self.session.login_pkce(fn_print)" not in patched:
    raise SystemExit("failed to patch tidal-dl-ng login method")
PY

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
