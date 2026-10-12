FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY prusa_rtsp_live/ ./prusa_rtsp_live/

RUN useradd --system --uid 10001 --no-create-home bridge
USER bridge

# Healthy while snapshots keep reaching Prusa Connect (or, with SNAPSHOTS=false,
# while the signaling link is up). Pair with autoheal to restart on a stall.
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; f=os.environ.get('HEALTH_FILE','/tmp/prusa-rtsp-live.health'); i=int(os.environ.get('SNAPSHOT_INTERVAL','10')); sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 3*i+90 else 1)"

ENTRYPOINT ["python", "-m", "prusa_rtsp_live"]
