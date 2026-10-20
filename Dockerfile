FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/build_sqlite.py /build/build_sqlite.py
RUN python /build/build_sqlite.py

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LD_LIBRARY_PATH=/opt/sqlite/lib SERVICEVOICE_DB=/data/servicevoice.sqlite3 \
    SERVICEVOICE_TELEPHONY_ENABLED=off SERVICEVOICE_JEV_FAQ=off \
    SERVICEVOICE_LANGSMITH_ENABLED=off SERVICEVOICE_ENVIRONMENT=production \
    SERVICEVOICE_LANGSMITH_BUDGET_PATH=/data/langsmith-budget.sqlite3
COPY --from=sqlite-build /opt/sqlite /opt/sqlite
WORKDIR /app
COPY requirements-lock.txt runtime_sqlite.py ./
RUN pip install --disable-pip-version-check --only-binary=:all: -r requirements-lock.txt \
    && pip check \
    && python -c "from runtime_sqlite import ensure_sqlite; assert ensure_sqlite() == '3.54.1'" \
    && groupadd --gid 10001 servicevoice \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin servicevoice \
    && mkdir /data && chown 10001:10001 /data
COPY app/ ./app/
COPY scripts/database_backup.py ./scripts/
COPY deploy/server.py deploy/runtime_health.py deploy/label_sandbox.py ./deploy/
COPY deploy/tts_server.py ./deploy/
RUN python deploy/label_sandbox.py
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 CMD ["python", "deploy/runtime_health.py"]
ENTRYPOINT ["python", "deploy/server.py"]
CMD ["--mode", "provider"]
