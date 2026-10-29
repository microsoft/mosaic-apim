# Build from the repository root: docker build -f demo/federal/Dockerfile .
FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS builder
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY demo/federal/requirements.txt .
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --index-url "${PIP_INDEX_URL}" --no-cache-dir \
      --require-hashes --only-binary=:all: -r requirements.txt

FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN addgroup --system federal && adduser --system --ingroup federal federal
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY e2e/mcp-servers/m-agent/m_agent/__init__.py ./m_agent/__init__.py
COPY e2e/mcp-servers/m-agent/m_agent/gateway.py ./m_agent/gateway.py
COPY e2e/mcp-servers/m-agent/m_agent/logs.py ./m_agent/logs.py
COPY demo/federal/federal_runtime ./federal_runtime
USER federal
EXPOSE 8000
CMD ["python", "-m", "federal_runtime.server"]
