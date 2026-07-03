from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers import admin, auth, cards, health, stream, voices

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Heim-AI Voice-Orchestrator", lifespan=lifespan)


@app.get("/")
def root() -> dict[str, str]:
    # Damit ein Browser-Aufruf der nackten Domain nicht als FastAPI-404
    # ("Not Found") erscheint, sondern zu den echten Endpoints fuehrt.
    return {
        "service": "heimai-orchestrator",
        "docs": "/docs",
        "health": "/v1/health",
        "admin": "/admin/",
    }


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(voices.router)
app.include_router(cards.router)
app.include_router(stream.router)
app.include_router(admin.router)

# Admin-Panel: statisches Vanilla-JS ohne Build-Step (Entscheidung 4.14).
# Die Seiten selbst sind oeffentlich, jede API-Aktion erfordert das
# Tier-3-Token (require_admin).
app.mount("/admin", StaticFiles(directory=_STATIC_DIR / "admin", html=True), name="admin")
