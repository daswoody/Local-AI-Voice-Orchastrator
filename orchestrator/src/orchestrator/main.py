from fastapi import FastAPI

from .routers import auth, cards, health, stream, voices

app = FastAPI(title="Heim-AI Voice-Orchestrator")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(voices.router)
app.include_router(cards.router)
app.include_router(stream.router)
