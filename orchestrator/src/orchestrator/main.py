from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers import admin, auth, cards, conversations, health, stream, voices

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Heim-AI Voice-Orchestrator", lifespan=lifespan)

# CORS: die Bootstrap-Seite der Windows-Shell laeuft unter tauri://localhost
# und prueft von dort /v1/health + /app/version.json. Auth laeuft ueber
# explizite Bearer-Header (keine Cookies), daher ist "*" hier vertretbar.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict[str, str]:
    # Damit ein Browser-Aufruf der nackten Domain nicht als FastAPI-404
    # ("Not Found") erscheint, sondern zu den echten Endpoints fuehrt.
    return {
        "service": "heimai-orchestrator",
        "docs": "/docs",
        "health": "/v1/health",
        "admin": "/admin/",
        "app": "/app/",
    }


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(voices.router)
app.include_router(cards.router)
app.include_router(conversations.router)
app.include_router(stream.router)
app.include_router(admin.router)

# Admin-Panel: statisches Vanilla-JS ohne Build-Step (Entscheidung 4.14).
# Die Seiten selbst sind oeffentlich, jede API-Aktion erfordert das
# Tier-3-Token (require_admin).
app.mount("/admin", StaticFiles(directory=_STATIC_DIR / "admin", html=True), name="admin")

# User-Web-UI (Phase 2.5, "voll zentral"): der gebaute Svelte-Client aus
# frontend/ wird vom Server ausgeliefert - die Windows-Shell (und jeder
# Browser) laedt die UI live von hier, ein Server-Deploy aktualisiert alle
# Clients. Im Dev-Checkout ohne Frontend-Build fehlt das Verzeichnis, dann
# bleibt der Mount einfach weg (Vite-Dev-Server uebernimmt).
_APP_DIR = _STATIC_DIR / "app"
if _APP_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=_APP_DIR, html=True), name="app")
