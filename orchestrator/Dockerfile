# Build-Context ist der REPO-ROOT (siehe docker-compose.yml) - so kann das
# Image die User-Web-UI aus frontend/ mitbauen ("voll zentral", Phase 2.5):
# ein Deploy aktualisiert Server UND UI fuer alle Clients.

# ---- Stage 1: User-Web-UI (Svelte/Vite) -----------------------------------
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# outDir ueberschreiben: der vite.config-Default zeigt auf den lokalen
# Orchestrator-Checkout, im Container sammeln wir nach /build/dist.
RUN npm run build -- --outDir /build/dist --emptyOutDir

# ---- Stage 2: Orchestrator (FastAPI) ---------------------------------------
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

# Erst nur die Abhaengigkeiten installieren (eigener Layer, cached solange sich
# pyproject.toml/uv.lock nicht aendern), dann den Quellcode kopieren und das
# Projekt selbst installieren.
COPY orchestrator/pyproject.toml orchestrator/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY orchestrator/src ./src
COPY --from=frontend /build/dist ./src/orchestrator/static/app
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

# --proxy-headers: hinter Coolify/Traefik terminiert der Proxy TLS; uvicorn
# uebernimmt Schema/Client-IP aus den X-Forwarded-Headern, damit die App
# https-Links generiert und Logs echte Client-IPs zeigen.
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
