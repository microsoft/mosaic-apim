FROM python:3.12-slim AS builder
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /workspace
COPY apps/api/requirements.lock apps/api/requirements.lock
COPY apps/api/src apps/api/src
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN python -m venv /workspace/.venv \
    && /workspace/.venv/bin/pip install --index-url "${PIP_INDEX_URL}" \
      --no-cache-dir --require-hashes \
      -r apps/api/requirements.lock \
    && /workspace/.venv/bin/pip install --index-url "${PIP_INDEX_URL}" \
      --no-cache-dir --no-deps ./apps/api

FROM python:3.12-slim AS runtime
ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN addgroup --system mosaic && adduser --system --ingroup mosaic mosaic
WORKDIR /workspace
COPY --from=builder --chown=mosaic:mosaic /workspace/.venv /workspace/.venv
USER mosaic
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["uvicorn", "mosaic_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
