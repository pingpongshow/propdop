# PropDop. CPU-bound: there is deliberately no CUDA stage, because the workload does not use
# the GPU (BUILD.md 4). All state lives on a bind-mounted host directory, not in the image.
FROM python:3.13-slim AS base

# libsndfile for soundfile; ffmpeg only for decoding container formats a node might hand us
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 ffmpeg alsa-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PROPDOP_DATA=/data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

# The container owns no state. Everything persistent is bind-mounted from
# the mounted host directory, so the image is disposable and the data survives a rebuild.
VOLUME ["/data"]

RUN useradd --create-home --uid 1000 propdop && chown -R propdop /app
USER propdop

EXPOSE 8475

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8475/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["propdop"]
CMD ["serve"]
