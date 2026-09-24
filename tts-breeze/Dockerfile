# Breeze TTS 2 (Test-Engine, v1.17): offizieller PyTorch-Streaming-Server
# (breeze_infer.api), den der Orchestrator direkt anspricht.
#
# Bewusst NICHT das Upstream-Dockerfile: Das baut FlashAttention aus dem
# Quellcode (devel-Image, lange Build-Zeit, viel RAM - zu viel fuer die
# 16-GB-VM) und FlashAttention 2 laeuft ohnehin erst ab Ampere, nicht auf der
# RTX 2080 Ti (Turing). Der Streaming-Server rechnet mit
# attn_implementation="eager" und braucht flash-attn gar nicht.
FROM python:3.11-slim

# soundfile/librosa/sox (Referenz-Sample einlesen) brauchen die
# Systembibliotheken; git fuer den Checkout des Inferenz-Codes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git libsndfile1 sox \
    && rm -rf /var/lib/apt/lists/*

# Der Inferenz-Code ist kein pip-Paket, sondern ein Repo (breeze_infer/ +
# models/) - auf einen Commit gepinnt, damit ein Rebuild nicht still eine
# andere Version zieht.
ARG BREEZE_REPO=https://github.com/breezeblue-ai/breeze-tts.git
ARG BREEZE_COMMIT=008f769016b0a24711becd7a4925030bc93f608c
RUN git clone "${BREEZE_REPO}" /opt/breeze \
    && git -C /opt/breeze checkout "${BREEZE_COMMIT}"
WORKDIR /opt/breeze

# Abhaengigkeiten exakt wie upstream (torch 2.9.1 mit CUDA-12.8-Wheels,
# qwen-tts, transformers 4.57.3), nur ohne die Entwickler-Tools. Das Image
# wird entsprechend gross, der erste Build dauert.
RUN grep -v -E '^(pytest|ruff)' requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY entrypoint.py /app/entrypoint.py

ENV PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/models/hf-home

EXPOSE 7860

# /health antwortet 503, solange das Modell laedt - davor kommt beim ersten
# Start der Download der Gewichte (mehrere GB): grosszuegige Start-Phase.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=1800s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:7860/health').status==200 else 1)"

ENTRYPOINT ["python", "/app/entrypoint.py"]
