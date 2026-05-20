FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Cursor file lives here by default; mount a volume to persist across restarts.
RUN mkdir -p /var/lib/pipe && chmod 0755 /var/lib/pipe

# Copy the framework + both connectors as source so the image can build
# directly from this repo without a published wheel.
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md
COPY src /app/src
COPY connectors/n8n /app/connectors/n8n
COPY connectors/aisquare-gateway /app/connectors/aisquare-gateway

RUN pip install /app /app/connectors/n8n /app/connectors/aisquare-gateway

COPY docker/pipe-n8n/entrypoint.py /usr/local/bin/pipe-n8n-entrypoint
RUN chmod +x /usr/local/bin/pipe-n8n-entrypoint

# Default cursor location is /var/lib/pipe so the brief's tmp default is
# overridden by mounting a volume here in production deployments.
ENV N8N_CURSOR_PATH=/var/lib/pipe/n8n-cursor.json

ENTRYPOINT ["/usr/local/bin/pipe-n8n-entrypoint"]
