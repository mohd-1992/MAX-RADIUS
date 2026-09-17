# ==============================================================================
# Production Dockerfile for WISP FreeRADIUS & MikroTik Manager (MySQL Edition)
# ==============================================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (curl, ping, default-mysql-client, docker.io, gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    default-mysql-client \
    docker.io \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create volume mount folders & set permissions
RUN mkdir -p /app/storage/backups /app/storage/uploads /app/storage/logs /app/database && \
    chmod +x /app/entrypoint.sh

# Expose Web Port (5090) & FreeRADIUS / CoA Ports (UDP)
EXPOSE 5090/tcp
EXPOSE 18120/udp
EXPOSE 18130/udp
EXPOSE 37990/udp

# Healthcheck for container vitality
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5090/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]