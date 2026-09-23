"""Saubere Filler-Vorgenerierung (v1.16).

Nutzer-Report: Filler endeten mit fremden Audiofetzen oder liefen in
Zeitlupe - der Realtime-Talk nicht. Hauptursache waren parallele
XTTS-Synthesen (Serialisierung: tts-xtts/tests/test_exclusive_synthesis.py).
Hier die Orchestrator-Seite: eine Generierung zur Zeit, Qualitaetspfad mit
sauberem Satzende, Plausibilitaetspruefung mit Neuversuch, einzelne Stimmen
neu generieren, atomares Schreiben."""

import asyncio
import math
from array import array
from pathlib import Path

import pytest

from orchestrator import repos
from orchestrator.config import settings
from orchestrator.services import filler_service

RATE = 24000
FEMALE, MALE = "default-de-female", "default-de-male"


def tone(seconds: float) -> bytes:
    """Gleichmaessig lautes Signal als Stellvertreter fuer Sprache."""
    return array("h", (int(8000 * math.sin(2 * math.pi * 220 * i / RATE))
                       for i in range(int(seconds * RATE)))).tobytes()


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


def _filler(text: str = "Moment bitte.", engine: str = "xtts") -> dict:
    trigger = repos.create_trigger(f"T-{engine}-{text}", "thinking", None)
    return repos.create_filler("Titel", text, trigger["id"], True, delay_ms=0, engine=engine)


def _samples(*voice_ids: str) -> None:
    voices_dir = Path(settings.voices_dir)
    voices_dir.mkdir(parents=True, exist_ok=True)
    for voice_id in voice_ids:
        (voices_dir / f"{voice_id}.wav").write_bytes(b"RIFF fake")


def _duration(filler_id: int, voice_id: str) -> float:
    pcm, rate = filler_service.load_audio(filler_service.audio_path(filler_id, voice_id))
    return len(pcm) / 2 / rate


class FakeXtts:
    """Liefert die vorgegebenen Aufnahmen der Reihe nach (die letzte
    wiederholt sich) und merkt sich (Text, Stimme, Temperatur)."""

    def __init__(self, *takes) -> None:
        self.takes = list(takes)
        self.calls: list[tuple] = []

    async def synthesize(self, text, voice_id, language=None, temperature=None):
        self.calls.append((text, voice_id, temperature))
        take = self.takes.pop(0) if len(self.takes) > 1 else self.takes[0]
        if isinstance(take, Exception):
            raise take
        return take, RATE


@pytest.fixture
def xtts(monkeypatch):
    def install(*takes) -> FakeXtts:
        fake = FakeXtts(*takes)
        monkeypatch.setattr(filler_service.xtts_client, "synthesize", fake.synthesize)
        return fake

    return install


# ---- Qualitaetspfad + Plausibilitaet --------------------------------------------


@pytest.mark.asyncio
async def test_xtts_gets_full_synthesis_with_a_clean_sentence_end(xtts):
    _samples(FEMALE)
    filler = _filler("Moment bitte")
    fake = xtts(tone(1.0))

    results = await filler_service.generate_audio(filler["id"], FEMALE)

    assert results == [{"voice_id": FEMALE, "ok": True, "attempts": 1}]
    assert fake.calls == [("Moment bitte.", FEMALE, None)]


@pytest.mark.asyncio
async def test_slow_motion_take_is_rolled_again_more_carefully(xtts):
    """Viel zu lang fuer den Text (Zeitlupe oder angehaengte Laute): neu
    wuerfeln mit niedrigerer Temperatur, die plausible Aufnahme gewinnt."""
    _samples(FEMALE)
    filler = _filler()
    fake = xtts(tone(6.0), tone(1.1))

    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result == {"voice_id": FEMALE, "ok": True, "attempts": 2}
    assert [temperature for _, _, temperature in fake.calls] == [None, 0.6]
    assert _duration(filler["id"], FEMALE) < 1.5


@pytest.mark.asyncio
async def test_least_suspicious_take_is_kept_with_a_warning(xtts):
    _samples(FEMALE)
    filler = _filler()
    fake = xtts(tone(6.0), tone(3.0), tone(8.0))

    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result["ok"] is True
    assert result["attempts"] == 3
    assert "auffaellig lang" in result["warning"]
    assert [temperature for _, _, temperature in fake.calls] == [None, 0.6, 0.45]
    assert 2.9 < _duration(filler["id"], FEMALE) < 3.3


@pytest.mark.asyncio
async def test_failing_retry_keeps_the_best_take_so_far(xtts):
    _samples(FEMALE)
    filler = _filler()
    xtts(tone(6.0), RuntimeError("XTTS-Fehler 503: belegt"))

    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert "warning" in result
    assert filler_service.has_audio(filler["id"], FEMALE)


@pytest.mark.asyncio
async def test_silent_take_counts_as_suspicious(xtts):
    _samples(FEMALE)
    filler = _filler()
    xtts(silence(1.0), tone(1.0))

    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result == {"voice_id": FEMALE, "ok": True, "attempts": 2}


@pytest.mark.asyncio
async def test_silence_around_the_speech_is_trimmed(xtts):
    _samples(FEMALE)
    filler = _filler()
    xtts(silence(0.8) + tone(1.0) + silence(1.5))

    await filler_service.generate_audio(filler["id"], FEMALE)

    # 60 ms Vorlauf + 1 s Sprache + 200 ms Nachlauf
    assert _duration(filler["id"], FEMALE) == pytest.approx(1.26, abs=0.02)


# ---- Einzelne Stimmen ---------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerating_one_voice_keeps_the_others(xtts):
    """Nutzerwunsch v1.16: War eine Stimme gut, darf sie nicht mit
    verschwinden, wenn eine andere neu gewuerfelt wird."""
    _samples(FEMALE, MALE)
    filler = _filler()
    xtts(tone(1.0))
    await filler_service.generate_audio(filler["id"])
    male_before = filler_service.audio_path(filler["id"], MALE).read_bytes()

    fake = xtts(tone(1.2))
    results = await filler_service.generate_audio(filler["id"], FEMALE)

    assert [r["voice_id"] for r in results] == [FEMALE]
    assert [voice for _, voice, _ in fake.calls] == [FEMALE]
    assert filler_service.audio_path(filler["id"], MALE).read_bytes() == male_before
    assert _duration(filler["id"], FEMALE) == pytest.approx(1.2, abs=0.01)  # neue Aufnahme


@pytest.mark.asyncio
async def test_failed_regeneration_keeps_the_previous_audio(xtts):
    _samples(FEMALE)
    filler = _filler()
    xtts(tone(1.0))
    await filler_service.generate_audio(filler["id"], FEMALE)
    before = filler_service.audio_path(filler["id"], FEMALE).read_bytes()

    xtts(RuntimeError("XTTS-Fehler 500: CUDA out of memory"))
    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result["ok"] is False
    assert "CUDA out of memory" in result["error"]
    assert filler_service.audio_path(filler["id"], FEMALE).read_bytes() == before


@pytest.mark.asyncio
async def test_unknown_voice_is_rejected():
    filler = _filler()
    with pytest.raises(ValueError, match="gibtsnicht"):
        await filler_service.generate_audio(filler["id"], "gibtsnicht")


@pytest.mark.asyncio
async def test_piper_can_regenerate_a_single_voice(monkeypatch):
    filler = _filler(engine="piper")

    async def fake_piper(text):
        return tone(1.0), 22050

    monkeypatch.setattr(filler_service.piper_client, "synthesize", fake_piper)

    results = await filler_service.generate_audio(filler["id"], MALE)

    assert results == [{"voice_id": MALE, "ok": True}]
    assert filler_service.has_audio(filler["id"], MALE)
    assert not filler_service.has_audio(filler["id"], FEMALE)


def test_generate_endpoint_takes_an_optional_voice(client, admin_headers, xtts):
    _samples(FEMALE, MALE)
    filler = _filler()
    fake = xtts(tone(1.0))

    response = client.post(
        f"/v1/admin/fillers/{filler['id']}/generate?voice_id={MALE}", headers=admin_headers
    )

    assert response.status_code == 200
    assert [r["voice_id"] for r in response.json()["results"]] == [MALE]
    assert [voice for _, voice, _ in fake.calls] == [MALE]

    unknown = client.post(
        f"/v1/admin/fillers/{filler['id']}/generate?voice_id=gibtsnicht", headers=admin_headers
    )
    assert unknown.status_code == 404


# ---- Nebenlaeufigkeit + Schreiben --------------------------------------------------


@pytest.mark.asyncio
async def test_generations_never_run_in_parallel(monkeypatch):
    """Mehrere Klicks auf "Audio generieren" - auch fuer verschiedene
    Filler - laufen nacheinander statt gleichzeitig gegen XTTS."""
    monkeypatch.setattr(filler_service, "_generation_lock", asyncio.Lock())
    _samples(FEMALE, MALE)
    first, second = _filler("Moment bitte."), _filler("Ich schaue kurz nach.")
    running = peak = 0

    async def slow_synthesize(text, voice_id, language=None, temperature=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return tone(1.0), RATE

    monkeypatch.setattr(filler_service.xtts_client, "synthesize", slow_synthesize)

    batches = await asyncio.gather(
        filler_service.generate_audio(first["id"]),
        filler_service.generate_audio(second["id"]),
        filler_service.generate_audio(first["id"], MALE),
    )

    assert peak == 1
    assert all(result["ok"] for batch in batches for result in batch)


@pytest.mark.asyncio
async def test_audio_is_dropped_when_the_text_changes_meanwhile(monkeypatch):
    """Wird der Filler waehrend der Generierung umgetextet, gehoert das
    Ergebnis nicht mehr zu ihm - sonst spielte der Turn den alten Satz."""
    _samples(FEMALE)
    filler = _filler()

    async def synthesize_while_admin_edits(text, voice_id, language=None, temperature=None):
        repos.update_filler(filler["id"], "Titel", "Ganz neuer Text.", filler["trigger_id"],
                            True, 0, "xtts")
        return tone(1.0), RATE

    monkeypatch.setattr(filler_service.xtts_client, "synthesize", synthesize_while_admin_edits)

    [result] = await filler_service.generate_audio(filler["id"], FEMALE)

    assert result["ok"] is False
    assert "geaendert" in result["error"]
    assert not filler_service.has_audio(filler["id"], FEMALE)


@pytest.mark.asyncio
async def test_writing_leaves_no_temp_files(xtts):
    _samples(FEMALE, MALE)
    filler = _filler()
    xtts(tone(1.0))

    await filler_service.generate_audio(filler["id"])

    names = sorted(p.name for p in Path(settings.filler_cache_dir).iterdir())
    assert names == [f"{filler['id']}_{FEMALE}.wav", f"{filler['id']}_{MALE}.wav"]


# ---- Textaufbereitung ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("Lass mich kurz nachdenken", "Lass mich kurz nachdenken."),
        ("Hm... lass mich ueberlegen...", "Hm, lass mich ueberlegen."),
        ("Moment…", "Moment."),
        ("Einen Augenblick,", "Einen Augenblick."),
        ("  Ich  schaue kurz nach.  ", "Ich schaue kurz nach."),
        ("Alles klar?", "Alles klar?"),
        ("Moment - ", "Moment."),
    ],
)
def test_prepare_text_gives_xtts_a_clean_sentence(raw, spoken):
    assert filler_service.prepare_text(raw) == spoken
