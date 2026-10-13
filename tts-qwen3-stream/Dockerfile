# Qwen3-TTS mit Streaming und GGUF-Quantisierung als Heim-AI-Sprachausgabe
# (v1.25) - Engine nach dem Engine-Vertrag (tts-engine-kit). Build-Context ist
# das Repo-Root, weil das Kit mit ins Image muss:
#   docker build -f tts-qwen3-stream/Dockerfile .
#
# Basis: CUDA-12.8-Runtime auf Ubuntu 24.04. Das qwentts.cpp-Wheel (CUDA 12.8)
# verlangt glibc >= 2.39 (also nicht Debian 12) und bringt libcudart/libcublas
# nicht mit - beides liefert dieses Image. libcuda.so.1 (Treiber) kommt zur
# Laufzeit vom NVIDIA-Container-Toolkit. Kein torch: Image ~2 GB statt ~6 GB.
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PYTHON=python3 UV_PYTHON_DOWNLOADS=never

WORKDIR /src/tts-qwen3-stream

COPY tts-engine-kit /src/tts-engine-kit
COPY tts-qwen3-stream/pyproject.toml tts-qwen3-stream/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra engine

COPY tts-qwen3-stream/src ./src
RUN uv sync --frozen --no-dev --extra engine

ENV PATH="/src/tts-qwen3-stream/.venv/bin:$PATH" \
    HF_HOME=/models/hf \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Health antwortet sofort; ob das Modell geladen ist, steht in /v1/device.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

CMD ["uvicorn", "tts_qwen3_stream.main:app", "--host", "0.0.0.0", "--port", "8000"]
