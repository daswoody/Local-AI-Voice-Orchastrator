from fastapi import FastAPI

from .routers import auth, cards, health, stream, voices

app = FastAPI(title="Heim-AI Voice-Orchestrator")


@app.get("/")
def root() -> dict[str, str]:
    # Damit ein Browser-Aufruf der nackten Domain nicht als FastAPI-404
    # ("Not Found") erscheint, sondern zu den echten Endpoints fuehrt.
    return {
        "service": "heimai-orchestrator",
        "docs": "/docs",
        "health": "/v1/health",
    }


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(voices.router)
app.include_router(cards.router)
app.include_router(stream.router)
