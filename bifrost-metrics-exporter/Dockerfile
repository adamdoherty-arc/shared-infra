# Tiny Prometheus exporter for Bifrost's logs.db SQLite mirror.
# Stays alpine-small (~80 MB) so it can run as a sidecar without
# meaningful overhead on the shared host.
FROM python:3.13-alpine

WORKDIR /app

RUN pip install --no-cache-dir prometheus_client==0.21.0

COPY exporter.py /app/exporter.py

# logs.db + config.db are bind-mounted read-only from
# shared-infra/bifrost/ via docker-compose.bifrost.yml. The exporter
# writes its read cursor to /state (a small named volume) so it can
# resume across container restarts.
VOLUME ["/data", "/state"]

EXPOSE 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null --tries=1 --timeout=3 http://127.0.0.1:9100/health || exit 1

CMD ["python", "-u", "/app/exporter.py"]
