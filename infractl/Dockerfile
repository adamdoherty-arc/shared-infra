# shared-infra-control — the infractl control-plane API/scheduler/CLI/UI.
# Build context is the REPO ROOT (docker-compose.control.yml sets
# `build: { context: ., dockerfile: infractl/Dockerfile }`) because this
# image also vendors scripts/export_config_snapshot.py, which lives outside
# infractl/ — Docker COPY cannot reach outside its build context, so a
# infractl/-scoped context could never pull it in.
FROM python:3.12-slim

WORKDIR /app

COPY infractl/pyproject.toml /app/pyproject.toml
COPY infractl /app/infractl

# Vendor scripts/export_config_snapshot.py so bifrost/config.py's
# render_redacted_snapshot()/write_redacted_snapshot() can import the SAME
# redaction logic (_key_ref/render) the pre-commit gate uses on the host,
# instead of re-implementing it — see infractl/bifrost/config.py's module
# docstring. INFRACTL_VENDORED_SCRIPTS_DIR defaults to this path.
COPY scripts/export_config_snapshot.py /app/vendor/scripts/export_config_snapshot.py

RUN pip install --no-cache-dir .

ENV INFRACTL_VENDORED_SCRIPTS_DIR=/app/vendor/scripts \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "infractl.app:app", "--host", "0.0.0.0", "--port", "8095"]
