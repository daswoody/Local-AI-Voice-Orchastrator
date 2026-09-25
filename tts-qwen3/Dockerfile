# Qwen3-TTS als Heim-AI-Sprachausgabe (v1.20) - erste Engine nach dem
# Engine-Vertrag (tts-engine-kit). Build-Context ist das Repo-Root, weil das
# Kit mit ins Image muss:
#   docker build -f tts-qwen3/Dockerfile .
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

# sox: qwen-tts importiert das sox-Paket beim Start (sonst nur eine Warnung).
RUN apt-get update && apt-get install -y --no-install-recommends sox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/tts-qwen3

COPY tts-engine-kit /src/tts-engine-kit
COPY tts-qwen3/pyproject.toml tts-qwen3/uv.lock ./
# torch bringt die CUDA-Bibliotheken als Wheels mit (gleiche Version wie
# tts-xtts) - Image ~6 GB, der erste Build dauert entsprechend.
RUN uv sync --frozen --no-install-project --no-dev --extra engine

COPY tts-qwen3/src ./src
RUN uv sync --frozen --no-dev --extra engine

ENV PATH="/src/tts-qwen3/.venv/bin:$PATH" \
    HF_HOME=/models/hf \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Health antwortet sofort; ob das Modell geladen ist, steht in /v1/device.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

CMD ["uvicorn", "tts_qwen3.main:app", "--host", "0.0.0.0", "--port", "8000"]
