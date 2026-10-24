# syntax=docker/dockerfile:1
# Build with --secret id=runtime_signing_key,src=/private/runtime-signing-private.pem
# The signing secret and compiler stage never enter customer image layers.
FROM python:3.11-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
COPY build-constraints.txt /tmp/build-constraints.txt
RUN pip install --no-cache-dir -c /tmp/build-constraints.txt -r requirements.txt && pip install --no-cache-dir cython==3.1.4 setuptools==80.9.0
COPY run.py entrypoint.sh ./
COPY core/ ./core/
COPY services/ ./services/
COPY deploy/hosted-max/update_controller.py ./services/system_update_host.py
COPY deploy/hosted-max/provision_update_controller.py ./services/system_update_provision.py
COPY web/ ./web/
COPY database/db.py database/schema_healer.py database/schema_objects.py database/mysql_bootstrap.py database/schema_mysql.sql ./database/
COPY database/freeradius_standard.sql database/wisp_extensions.sql database/seed_data.sql ./database/
COPY database/license_capacity.py database/maintenance_jobs.py ./database/
COPY database/permission_catalog.py database/quick_card_schema.py database/first_use_policy.py ./database/
COPY database/userman_import_schema.py ./database/
COPY database/loyalty_schema.py database/cycle_safety.py database/nas_session_safety.py database/voucher_sales_schema.py ./database/
COPY database/migrations/ ./database/migrations/
COPY storage/keys/master_public_key.pem ./storage/keys/master_public_key.pem
COPY packaging/ /opt/max-build/
RUN python -c "from setuptools import setup, Extension; from Cython.Build import cythonize; names=['services.license_guard_service','core.licensing','core.hardware_fingerprint','core.license_protocol','core.license_security','database.license_capacity']; setup(name='SecurityCore', ext_modules=cythonize([Extension(n, [n.replace('.','/')+'.py']) for n in names], compiler_directives={'language_level':'3'}), script_args=['build_ext','--inplace'])" && \
    rm -f services/license_guard_service.py core/licensing.py core/hardware_fingerprint.py core/license_protocol.py core/license_security.py database/license_capacity.py services/*.c core/*.c database/*.c && \
    rm -rf build
RUN pip install --no-cache-dir rjsmin==1.2.4 rcssmin==1.2.2 && \
    python /opt/max-build/minify_assets.py && \
    python /opt/max-build/embed_templates.py && \
    python /opt/max-build/compile_runtime.py && \
    rm -rf /app/web/templates && \
    pip uninstall -y cython setuptools rjsmin rcssmin
RUN --mount=type=secret,id=runtime_signing_key,required=true python /opt/max-build/seal_runtime.py
RUN cp /opt/max-build/verify_runtime.py /opt/max-protection/verify.py

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl iputils-ping iproute2 default-mysql-client docker.io chromium fonts-noto-core && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
COPY --from=build /opt/max-protection /opt/max-protection
RUN mkdir -p /app/storage/backups /app/storage/uploads /app/storage/logs && chmod +x /app/entrypoint.sh
LABEL org.opencontainers.image.version="2.6.5" max.release.channel="control-v1"
EXPOSE 80/tcp 5090/tcp 18120/udp 18130/udp 37990/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD curl -f http://localhost:5090/ || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
