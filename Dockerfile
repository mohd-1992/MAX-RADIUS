# Compile in a disposable stage; protected sources never enter runtime layers.
FROM python:3.11-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir cython setuptools
COPY run.py entrypoint.sh ./
COPY core/ ./core/
COPY services/ ./services/
COPY web/ ./web/
COPY database/db.py database/schema_healer.py database/schema_mysql.sql ./database/
COPY database/freeradius_standard.sql database/wisp_extensions.sql database/seed_data.sql ./database/
COPY database/license_capacity.py database/maintenance_jobs.py ./database/
COPY database/loyalty_schema.py ./database/
COPY database/migrations/ ./database/migrations/
COPY storage/keys/master_public_key.pem ./storage/keys/master_public_key.pem
RUN python -c "from setuptools import setup, Extension; from Cython.Build import cythonize; names=['services.license_guard_service','core.licensing','core.hardware_fingerprint','core.license_protocol','core.license_security','database.license_capacity']; setup(name='SecurityCore', ext_modules=cythonize([Extension(n, [n.replace('.','/')+'.py']) for n in names], compiler_directives={'language_level':'3'}), script_args=['build_ext','--inplace'])" && \
    rm -f services/license_guard_service.py core/licensing.py core/hardware_fingerprint.py core/license_protocol.py core/license_security.py database/license_capacity.py services/*.c core/*.c database/*.c && \
    rm -rf build && pip uninstall -y cython setuptools

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl iputils-ping iproute2 default-mysql-client docker.io && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
RUN mkdir -p /app/storage/backups /app/storage/uploads /app/storage/logs && chmod +x /app/entrypoint.sh
EXPOSE 80/tcp 5090/tcp 18120/udp 18130/udp 37990/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD curl -f http://localhost:5090/ || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
