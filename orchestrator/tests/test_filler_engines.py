"""Filler-Generierung mit der gewaehlten Engine (v1.15)."""

from unittest.mock import AsyncMock

import pytest

from orchestrator import repos
from orchestrator.services import filler_service


def _filler(engine: str) -> dict:
    trigger = repos.create_trigger(f"T-{engine}", "thinking", None)
    return repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                               delay_ms=0, engine=engine)


@pytest.mark.asyncio
async def test_piper_engine_writes_one_audio_for_every_voice(client, monkeypatch):
    """Piper hat genau EINE Stimme - dasselbe Audio wird fuer alle Stimmen
    abgelegt, damit der Abspielpfad (Cache-Matrix) unveraendert bleibt."""
    filler = _filler("piper")
    piper = AsyncMock(return_value=(b"\x01\x02" * 800, 22050))
    monkeypatch.setattr(filler_service.piper_client, "synthesize", piper)
    xtts = AsyncMock(side_effect=AssertionError("XTTS darf hier nicht laufen"))
    monkeypatch.setattr(filler_service.xtts_client, "stream", xtts)

    results = await filler_service.generate_audio(filler["id"])

    assert all(r["ok"] for r in results)
    piper.assert_awaited_once_with("Moment bitte.")
    for voice in repos.list_voices():
        assert filler_service.has_audio(filler["id"], voice["id"])


@pytest.mark.asyncio
async def test_piper_failure_is_reported_per_voice(client, monkeypatch):
    filler = _filler("piper")
    monkeypatch.setattr(
        filler_service.piper_client, "synthesize",
        AsyncMock(side_effect=RuntimeError("Piper-Container down")),
    )

    results = await filler_service.generate_audio(filler["id"])

    assert results and not any(r["ok"] for r in results)
    assert "Piper-Container down" in results[0]["error"]


@pytest.mark.asyncio
async def test_xtts_engine_still_generates_per_voice(client, monkeypatch):
    """Default-Pfad bleibt unveraendert: XTTS spricht in der jeweiligen
    Nutzerstimme (Voice-Sample vorausgesetzt)."""
    from pathlib import Path

    from orchestrator.config import settings

    filler = _filler("xtts")
    voices_dir = Path(settings.voices_dir)
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "default-de-female.wav").write_bytes(b"RIFF fake")

    async def fake_synthesize(text, voice_id, language=None, temperature=None):
        return b"\x03\x04" * 12000, 24000

    monkeypatch.setattr(filler_service.xtts_client, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        filler_service.piper_client, "synthesize",
        AsyncMock(side_effect=AssertionError("Piper darf hier nicht laufen")),
    )

    results = await filler_service.generate_audio(filler["id"])

    by_voice = {r["voice_id"]: r for r in results}
    assert by_voice["default-de-female"]["ok"] is True
    # Stimme ohne Sample wird klar benannt statt still uebersprungen
    assert by_voice["default-de-male"]["ok"] is False
    assert "Voice-Sample" in by_voice["default-de-male"]["error"]
