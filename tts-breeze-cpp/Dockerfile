# Breeze TTS 2 als Breeze-TTS-2.cpp (v1.18): C++/GGUF-Portierung auf ggml -
# quantisiert (Q8_0 ~4 GB VRAM statt ~7,7 GB beim PyTorch-Server) und ohne
# bf16/FlashAttention, laeuft damit auch auf der RTX 2080 Ti (Turing). Hier mit
# dem CUDA-Backend gebaut; der Server spricht dieselbe HTTP-Schnittstelle wie
# der offizielle (tts-breeze/), der Orchestrator merkt keinen Unterschied.

ARG CUDA_VERSION=12.8.1

# ---- Build -------------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake git ninja-build \
    && rm -rf /var/lib/apt/lists/*

# Original-Repo (HoppouAI) auf einen Commit gepinnt, ggml kommt als Submodul.
ARG BREEZE_CPP_REPO=https://github.com/HoppouAI/Breeze-TTS-2.cpp.git
ARG BREEZE_CPP_COMMIT=a5436642d4c64304b398ceeda9b8fce4577bfdb1
RUN git clone "${BREEZE_CPP_REPO}" /src \
    && git -C /src checkout "${BREEZE_CPP_COMMIT}" \
    && git -C /src submodule update --init --recursive

# CUDA-Architekturen der eigenen Karten (nvidia-smi --query-gpu=compute_cap
# --format=csv): 61 = Pascal (GTX 10xx), 75 = Turing (RTX 20xx, 2080 Ti),
# 86 = Ampere (RTX 30xx), 89 = Ada (RTX 40xx). Weniger = schnellerer Build.
ARG CUDA_ARCHS="61;75;86;89"
# nvcc braucht pro Job viel RAM - auf der 16-GB-VM nicht alle Kerne auf einmal.
ARG BUILD_JOBS=4
WORKDIR /src
# Statisch gelinkt: ein einziges Binary, das Laufzeit-Image braucht nur noch
# die CUDA-Bibliotheken. --allow-shlib-undefined: libcuda kommt erst zur
# Laufzeit vom NVIDIA-Container-Toolkit, gebaut wird gegen den Stub.
RUN cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DBREEZE_CUDA=ON -DBREEZE_VULKAN=OFF \
        -DBREEZE_BUILD_CLI=OFF -DBREEZE_BUILD_SHARED=OFF \
        -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHS}" \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,--allow-shlib-undefined" \
    && cmake --build build --target breeze-server -j "${BUILD_JOBS}"

# ---- Laufzeit ------------------------------------------------------------------
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04

# libgomp: OpenMP fuer die CPU-Anteile von ggml; python3 fuer den Download
# der Gewichte im Entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libgomp1 python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/breeze-server /usr/local/bin/breeze-server
COPY entrypoint.py /app/entrypoint.py

EXPOSE 7860

# /health antwortet erst, wenn das Modell geladen ist; davor laedt der erste
# Start die Gewichte (Q8_0 ~3,3 GB) - grosszuegige Start-Phase.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=900s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:7860/health').status==200 else 1)"

ENTRYPOINT ["python3", "/app/entrypoint.py"]
