# ==============================================================================
# Production Dockerfile for WISP FreeRADIUS & MikroTik Manager (MySQL Edition)
# ==============================================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (curl, ping, default-mysql-client, docker.io, gcc, build-essential)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    default-mysql-client \
    docker.io \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python packages & Cython compiler
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir cython setuptools && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Compile critical security modules to native .so binaries and remove plaintext .py files
RUN python -c "from setuptools import setup, Extension; from Cython.Build import cythonize; setup(name='SecurityCore', ext_modules=cythonize([Extension('services.license_guard_service', ['services/license_guard_service.py']), Extension('core.licensing', ['core/licensing.py']), Extension('core.hardware_fingerprint', ['core/hardware_fingerprint.py'])], compiler_directives={'language_level':'3'}), script_args=['build_ext', '--inplace'])" && \
    rm -f services/license_guard_service.py core/licensing.py core/hardware_fingerprint.py services/*.c core/*.c && \
    rm -rf build

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