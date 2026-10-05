FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS builder
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --index-url "${PIP_INDEX_URL}" --no-cache-dir \
      --require-hashes --only-binary=:all: -r requirements.txt

FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed AS runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN addgroup --system mcp && adduser --system --ingroup mcp mcp
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY m_tools ./m_tools
USER mcp
EXPOSE 8000
CMD ["python", "-m", "m_tools"]
