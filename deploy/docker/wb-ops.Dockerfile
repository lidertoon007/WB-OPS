# Build with a clean context containing only requirements-prod.txt.
# Application source and credentials enter only as runtime mounts/env.
FROM python:3.12.3-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements-prod.txt /tmp/requirements-prod.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir \
      -r /tmp/requirements-prod.txt \
    && rm /tmp/requirements-prod.txt

USER 999:988
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
