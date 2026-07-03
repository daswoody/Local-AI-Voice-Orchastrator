FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra engine

COPY src ./src
RUN uv sync --frozen --no-dev --extra engine

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

# Entrypoint laedt das Voice-Modell ins /models-Volume (falls fehlt) und
# startet dann uvicorn.
CMD ["python", "-m", "tts_piper.entrypoint"]
