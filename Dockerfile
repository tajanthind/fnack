FROM python:3.11-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs chromium xvfb procps curl unzip && \
    curl -fsSL https://deno.land/install.sh | sh && \
    cp /root/.deno/bin/deno /usr/local/bin/deno && \
    rm -rf /var/lib/apt/lists/*

ENV CHROME_PATH=/usr/bin/chromium \
    DISPLAY=:99 \
    SPOTIFLAC_REGISTRIES=https://raw.githubusercontent.com/spotiflacapp/SpotiFLAC-Extension/refs/heads/main/registry.json \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /config /downloads /music
VOLUME ["/config", "/downloads", "/music"]

EXPOSE 4688

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:4688/api/artists || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "-k", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "1", "-b", "0.0.0.0:4688", "app:app"]