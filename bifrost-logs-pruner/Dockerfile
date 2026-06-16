# Minimal Python container for the Bifrost logs.db nightly pruner.
# Needs read-write access to /app/data/logs.db inside shared-bifrost.
# No external dependencies beyond stdlib sqlite3.
FROM python:3.13-alpine

WORKDIR /app

COPY pruner.py /app/pruner.py

# /data is the bind-mount for bifrost/ (read-write, unlike the exporter's :ro).
VOLUME ["/data"]

CMD ["python", "-u", "/app/pruner.py"]
