FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

# Erst nur die Abhaengigkeiten installieren (eigener Layer, cached solange sich
# pyproject.toml/uv.lock nicht aendern), dann den Quellcode kopieren und das
# Projekt selbst installieren.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/v1/health').status==200 else 1)"

# --proxy-headers: hinter Coolify/Traefik terminiert der Proxy TLS; uvicorn
# uebernimmt Schema/Client-IP aus den X-Forwarded-Headern, damit die App
# https-Links generiert und Logs echte Client-IPs zeigen.
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
