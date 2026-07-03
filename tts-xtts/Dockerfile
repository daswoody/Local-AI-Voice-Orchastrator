FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
# coqui-tts zieht torch mit CUDA-Wheels - Image wird ~5-6 GB gross,
# Build auf der VM dauert entsprechend (einmalig).
RUN uv sync --frozen --no-install-project --no-dev --extra engine

COPY src ./src
RUN uv sync --frozen --no-dev --extra engine

ENV PATH="/app/.venv/bin:$PATH"
# XTTS-v2 steht unter der Coqui Public Model License (nicht-kommerziell -
# fuer das private Heim-AI-Projekt ok). Der Auto-Download beim ersten
# Start fragt sonst interaktiv nach Zustimmung.
ENV COQUI_TOS_AGREED=1
# Modell-Cache ins Volume (docker-compose haengt /models ein).
ENV TTS_HOME=/models

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=120s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

CMD ["uvicorn", "tts_xtts.main:app", "--host", "0.0.0.0", "--port", "8000"]
