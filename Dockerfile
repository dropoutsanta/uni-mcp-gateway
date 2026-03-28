FROM golang:1.25-bookworm AS go-builder
WORKDIR /build
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/ ./
RUN CGO_ENABLED=1 GOOS=linux go build -o /whatsapp-bridge .

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY --from=go-builder /whatsapp-bridge /app/whatsapp-bridge
COPY pyproject.toml .
RUN uv pip install --system --no-cache -r pyproject.toml
COPY *.py ./
COPY plugins/ ./plugins/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

RUN mkdir -p /data/store /data/store2 /data/results

# Each bridge instance gets its own working dir with a store/ symlink
# pointing to its persistent data directory
RUN mkdir -p /app/bridge-default /app/bridge-second
RUN ln -s /data/store /app/bridge-default/store
RUN ln -s /data/store2 /app/bridge-second/store

EXPOSE 8080
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
