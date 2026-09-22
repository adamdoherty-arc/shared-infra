# Tiny Prometheus exporter for Bifrost's logs store (SQLite logs.db, or
# Postgres via BIFROST_LOGS_STORE_TYPE=postgres -- 2026-09-22 lock-storm fix).
# 2026-09-22: switched alpine -> slim. psycopg2-binary ships manylinux
# (glibc) wheels only; alpine's musl libc has no matching wheel and pip
# would fall back to a from-source build needing postgresql-dev + gcc +
# musl-dev, so alpine here would either fail to build or need those
# packages added anyway, erasing the size win. slim keeps a real wheel
# install and stays small relative to the image budget for a sidecar.
FROM python:3.13-slim

WORKDIR /app

# wget is alpine's default net-tool but slim (debian) ships neither wget nor
# curl -- the HEALTHCHECK below needs one of them.
RUN apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir prometheus_client==0.21.0 psycopg2-binary==2.9.10

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
