# OpsPilot API and operator console (one image, two commands). Replay mode by default;
# Ollama stays on the host and is reached through host.docker.internal.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY knowledge ./knowledge
COPY faults ./faults
COPY evals ./evals
COPY .streamlit ./.streamlit

RUN useradd --uid 10001 --create-home app && mkdir -p runs data && chown -R app runs data
USER 10001

ENV OPSPILOT_API_HOST=0.0.0.0 \
    OPSPILOT_API_MODE=replay \
    OPSPILOT_OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OPSPILOT_WEAVIATE_URL=http://weaviate:8080 \
    OPSPILOT_WEAVIATE_GRPC_PORT=50051
EXPOSE 8000 8501
CMD ["opspilot-api"]
