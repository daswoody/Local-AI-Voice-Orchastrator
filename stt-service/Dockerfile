FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
# engine = faster-whisper; gpu = cuBLAS/cuDNN als pip-Pakete, damit kein
# CUDA-Basis-Image noetig ist (dokumentierter Weg aus dem
# faster-whisper-README - auf der VM verifizieren).
RUN uv sync --frozen --no-install-project --no-dev --extra engine --extra gpu

COPY src ./src
RUN uv sync --frozen --no-dev --extra engine --extra gpu

ENV PATH="/app/.venv/bin:$PATH"
# ctranslate2 sucht cuBLAS/cuDNN ueber den Library-Pfad; die pip-Pakete
# legen ihre .so-Dateien unter site-packages/nvidia/*/lib ab.
ENV LD_LIBRARY_PATH="/app/.venv/lib/python3.11/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.11/site-packages/nvidia/cudnn/lib"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

CMD ["uvicorn", "stt_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
