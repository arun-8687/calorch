# Calorch — Dockerfile for Azure Container Apps
#
# Multi-stage build. Final image is ~180 MB (python:3.12-slim + deps).
# Run as non-root. Health check via /health endpoint exposed by the app.

FROM python:3.12-slim AS builder
WORKDIR /build

# Build deps for cryptography, lxml, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libffi-dev libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build \
    && pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash calorch

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels calorch \
    && rm -rf /wheels

USER calorch
WORKDIR /home/calorch

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    OUTPUT_DIR=/data/out

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "calorch.serve:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
