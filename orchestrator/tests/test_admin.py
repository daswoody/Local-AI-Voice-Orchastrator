import io
import wave

from orchestrator import repos
from orchestrator.services import filler_service


def _wav_bytes(rate=22050, seconds=0.1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


# ---- Zugriffsschutz ----------------------------------------------------------


def test_admin_routes_require_token(client):
    assert client.get("/v1/admin/users").status_code == 401


def test_admin_routes_reject_non_admin(client, admin_headers):
    client.post(
        "/v1/admin/users",
        json={"username": "gast", "password": "pw", "tier": 1},
        headers=admin_headers,
    )
    login = client.post(
        "/v1/auth/login", json={"username": "gast", "password": "pw", "device_name": "t"}
    )
    gast_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/v1/admin/users", headers=gast_headers).status_code == 403


# ---- Users -------------------------------------------------------------------


def test_user_crud_roundtrip(client, admin_headers):
    created = client.post(
        "/v1/admin/users",
        json={"username": "frau", "password": "geheim", "display_name": "Frau", "tier": 2},
        headers=admin_headers,
    )
    assert created.status_code == 201
    user_id = created.json()["id"]

    login = client.post(
        "/v1/auth/login", json={"username": "frau", "password": "geheim", "device_name": "handy"}
    )
    assert login.status_code == 200
    assert login.json()["user"]["tier"] == 2

    updated = client.put(
        f"/v1/admin/users/{user_id}",
        json={"system_prompt_override": "Du bist besonders knapp."},
        headers=admin_headers,
    )
    assert updated.json()["system_prompt_override"] == "Du bist besonders knapp."
    assert repos.effective_system_prompt("frau") == "Du bist besonders knapp."

    assert client.delete(f"/v1/admin/users/{user_id}", headers=admin_headers).status_code == 204


def test_last_admin_cannot_be_deleted(client, admin_headers):
    admin = next(u for u in client.get("/v1/admin/users", headers=admin_headers).json() if u["tier"] == 3)
    response = client.delete(f"/v1/admin/users/{admin['id']}", headers=admin_headers)
    assert response.status_code == 400


# ---- Charakter -----------------------------------------------------------------


def test_character_global_and_override(client, admin_headers):
    client.put(
        "/v1/admin/character",
        json={"prompt": "Du bist Jarvis.", "voice_summary_prompt": "Fasse als Jarvis zusammen."},
        headers=admin_headers,
    )
    body = client.get("/v1/admin/character", headers=admin_headers).json()
    assert body == {"prompt": "Du bist Jarvis.",
                    "voice_summary_prompt": "Fasse als Jarvis zusammen."}
    # Ohne Override greift der globale Prompt
    assert repos.effective_system_prompt(None) == "Du bist Jarvis."
    assert repos.voice_summary_prompt() == "Fasse als Jarvis zusammen."


def test_empty_voice_summary_prompt_falls_back_to_default(client, admin_headers):
    client.put(
        "/v1/admin/character",
        json={"prompt": "Egal.", "voice_summary_prompt": ""},
        headers=admin_headers,
    )
    # Leer gespeichert -> eingebauter Default greift (inkl. Verbot von
    # Emotions-Tags, die XTTS woertlich vorlesen wuerde)
    assert "[froehlich]" in repos.voice_summary_prompt()


# ---- Stimmen --------------------------------------------------------------------


def test_voice_create_upload_delete(client, admin_headers):
    assert client.post(
        "/v1/admin/voices", json={"id": "papa", "name": "Papa"}, headers=admin_headers
    ).status_code == 201

    upload = client.post(
        "/v1/admin/voices/papa/sample",
        files={"file": ("papa.wav", _wav_bytes(), "audio/wav")},
        headers=admin_headers,
    )
    assert upload.status_code == 200

    voices = client.get("/v1/admin/voices", headers=admin_headers).json()
    papa = next(v for v in voices if v["id"] == "papa")
    assert papa["has_sample"] is True

    # kein RIFF-Header -> abgelehnt
    bad = client.post(
        "/v1/admin/voices/papa/sample",
        files={"file": ("x.mp3", b"ID3xxxx", "audio/mpeg")},
        headers=admin_headers,
    )
    assert bad.status_code == 400

    assert client.delete("/v1/admin/voices/papa", headers=admin_headers).status_code == 204


def test_voice_id_path_traversal_rejected(client, admin_headers):
    response = client.post(
        "/v1/admin/voices", json={"id": "../etc/passwd", "name": "Boese"}, headers=admin_headers
    )
    assert response.status_code == 400


# ---- Trigger & Filler -------------------------------------------------------------


def test_trigger_and_filler_crud(client, admin_headers):
    trigger = client.post(
        "/v1/admin/triggers",
        json={"name": "Kalender", "kind": "tool", "tool_pattern": "Calendar-*"},
        headers=admin_headers,
    )
    assert trigger.status_code == 201
    trigger_id = trigger.json()["id"]

    filler = client.post(
        "/v1/admin/fillers",
        json={"title": "Kalender-Blick", "text": "Ich schaue kurz in den Kalender.",
              "trigger_id": trigger_id},
        headers=admin_headers,
    )
    assert filler.status_code == 201
    assert filler.json()["trigger_kind"] == "tool"

    # Trigger loeschen -> Filler verschwindet mit (CASCADE)
    client.delete(f"/v1/admin/triggers/{trigger_id}", headers=admin_headers)
    titles = [f["title"] for f in client.get("/v1/admin/fillers", headers=admin_headers).json()]
    assert "Kalender-Blick" not in titles


def test_select_filler_prefers_specific_tool_pattern():
    generic = repos.create_trigger("Tool allgemein2", "tool", "*")
    calendar = repos.create_trigger("Kalender", "tool", "Calendar-*")
    repos.create_filler("Allgemein", "Moment.", generic["id"], True)
    repos.create_filler("Kalender-Blick", "Ich schaue in den Kalender.", calendar["id"], True)

    chosen = filler_service.select_filler("tool", "irgendeine-stimme", tool_name="Calendar-list_events")
    assert chosen["title"] == "Kalender-Blick"

    chosen = filler_service.select_filler("tool", "irgendeine-stimme", tool_name="Notes-search")
    assert chosen["title"] in ("Allgemein", "Ich kuemmere mich")


def test_filler_text_change_invalidates_audio(client, admin_headers, tmp_path):
    fillers = client.get("/v1/admin/fillers", headers=admin_headers).json()
    filler = fillers[0]

    # Fake-Audio in den Cache legen
    path = filler_service.audio_path(filler["id"], "default-de-female")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())
    assert filler_service.has_audio(filler["id"], "default-de-female")

    client.put(
        f"/v1/admin/fillers/{filler['id']}",
        json={"title": filler["title"], "text": "Ganz neuer Text.",
              "trigger_id": filler["trigger_id"], "enabled": True},
        headers=admin_headers,
    )
    assert not filler_service.has_audio(filler["id"], "default-de-female")


def _tone_pcm(seconds: float, rate: int = 24000) -> bytes:
    import math
    from array import array

    return array("h", (int(8000 * math.sin(2 * math.pi * 220 * i / rate))
                       for i in range(int(seconds * rate)))).tobytes()


def test_generate_filler_writes_wav_for_voices_with_sample(client, admin_headers, monkeypatch):
    from orchestrator.services import filler_service as fs

    spoken = _tone_pcm(1.6)

    async def fake_synthesize(text, voice_id, language=None, temperature=None):
        return spoken, 24000

    monkeypatch.setattr(fs.xtts_client, "synthesize", fake_synthesize)

    # Nur default-de-female bekommt ein Sample
    from orchestrator.config import settings
    from pathlib import Path
    voices_dir = Path(settings.voices_dir)
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "default-de-female.wav").write_bytes(_wav_bytes())

    filler_id = client.get("/v1/admin/fillers", headers=admin_headers).json()[0]["id"]
    result = client.post(f"/v1/admin/fillers/{filler_id}/generate", headers=admin_headers).json()

    by_voice = {r["voice_id"]: r for r in result["results"]}
    assert by_voice["default-de-female"]["ok"] is True
    assert by_voice["default-de-male"]["ok"] is False  # kein Sample
    assert filler_service.has_audio(filler_id, "default-de-female")
    # Generiertes WAV ist lesbar und hat die XTTS-Rate; die Sprache reicht
    # bis an beide Enden, also wird nur ein-/ausgeblendet, nichts gekappt.
    pcm, rate = filler_service.load_audio(filler_service.audio_path(filler_id, "default-de-female"))
    assert rate == 24000
    assert len(pcm) == len(spoken)


# ---- Karten ------------------------------------------------------------------------


def test_card_admin_crud_bumps_version(client, admin_headers):
    before = client.get("/v1/cards/layouts").json()["version"]

    client.post(
        "/v1/admin/cards",
        json={"card_type": "shopping_list", "root": {"type": "column", "children": []}},
        headers=admin_headers,
    )
    after = client.get("/v1/cards/layouts").json()
    assert after["version"] == before + 1
    assert "shopping_list" in [t["card_type"] for t in after["layouts"]]

    # since_version liefert nur die neue Karte
    delta = client.get(f"/v1/cards/layouts?since_version={before}").json()
    assert [t["card_type"] for t in delta["layouts"]] == ["shopping_list"]

    assert client.delete("/v1/admin/cards/shopping_list", headers=admin_headers).status_code == 204


# ---- Modelle -------------------------------------------------------------------------


def test_models_panel_lists_litellm_models_with_active_flag(client, admin_headers, monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator.routers import admin as admin_module

    monkeypatch.setattr(
        admin_module.litellm_client, "list_models",
        AsyncMock(return_value=[{"id": "gemma-4-e4b"}, {"id": "qwen3-8b"}]),
    )
    response = client.get("/v1/admin/models", headers=admin_headers)
    body = response.json()
    assert [m["id"] for m in body["models"]] == ["gemma-4-e4b", "qwen3-8b"]
    # Ohne Panel-Auswahl gilt der .env-Fallback
    assert body["active_model"] == "gemma-4-e4b"


def test_activate_model_switches_llm_calls(client, admin_headers, monkeypatch):
    from unittest.mock import AsyncMock
    from orchestrator import repos
    from orchestrator.routers import admin as admin_module
    from orchestrator.services.litellm_client import litellm_client

    monkeypatch.setattr(
        admin_module.litellm_client, "list_models",
        AsyncMock(return_value=[{"id": "gemma-4-e4b"}, {"id": "qwen3-8b"}]),
    )

    response = client.post(
        "/v1/admin/models/activate", json={"model": "qwen3-8b"}, headers=admin_headers
    )
    assert response.json() == {"active_model": "qwen3-8b"}
    assert repos.get_setting("active_model") == "qwen3-8b"
    # Der LLM-Client loest das aktive Modell pro Call auf
    assert litellm_client.active_model() == "qwen3-8b"

    # Unbekanntes Modell wird abgelehnt
    bad = client.post(
        "/v1/admin/models/activate", json={"model": "gibtsnicht"}, headers=admin_headers
    )
    assert bad.status_code == 404


def test_filler_delay_ms_crud(client, admin_headers):
    trigger_id = client.get("/v1/admin/triggers", headers=admin_headers).json()[0]["id"]

    created = client.post(
        "/v1/admin/fillers",
        json={"title": "Schnell", "text": "Moment.", "trigger_id": trigger_id, "delay_ms": 300},
        headers=admin_headers,
    ).json()
    assert created["delay_ms"] == 300

    updated = client.put(
        f"/v1/admin/fillers/{created['id']}",
        json={"title": "Schnell", "text": "Moment.", "trigger_id": trigger_id,
              "enabled": True, "delay_ms": 2500},
        headers=admin_headers,
    ).json()
    assert updated["delay_ms"] == 2500

    listed = client.get("/v1/admin/fillers", headers=admin_headers).json()
    assert next(f for f in listed if f["id"] == created["id"])["delay_ms"] == 2500

    # Negativer Delay wird abgelehnt
    bad = client.post(
        "/v1/admin/fillers",
        json={"title": "X", "text": "Y", "trigger_id": trigger_id, "delay_ms": -5},
        headers=admin_headers,
    )
    assert bad.status_code == 422


# ---- Filler: Engine-Wahl + Audio anhoeren (v1.15) ----------------------------


def _create_filler(client, admin_headers, engine="xtts", text="Moment bitte."):
    trigger = client.get("/v1/admin/triggers", headers=admin_headers).json()[0]
    response = client.post(
        "/v1/admin/fillers",
        json={"title": "Test", "text": text, "trigger_id": trigger["id"],
              "delay_ms": 500, "engine": engine},
        headers=admin_headers,
    )
    assert response.status_code == 201
    return response.json()


def test_tts_engines_are_listed_for_the_dropdown(client, admin_headers):
    engines = client.get("/v1/admin/tts-engines", headers=admin_headers).json()
    by_id = {engine["id"]: engine for engine in engines}
    assert set(by_id) == {"xtts", "piper"}
    # per_voice steuert, ob die Engine in der Nutzerstimme spricht
    assert by_id["xtts"]["per_voice"] is True
    assert by_id["piper"]["per_voice"] is False


def test_filler_engine_defaults_to_xtts_and_is_stored(client, admin_headers):
    default = _create_filler(client, admin_headers)
    assert default["engine"] == "xtts"

    piper = _create_filler(client, admin_headers, engine="piper")
    assert piper["engine"] == "piper"

    listed = {f["id"]: f for f in client.get("/v1/admin/fillers", headers=admin_headers).json()}
    assert listed[piper["id"]]["engine"] == "piper"


def test_unknown_engine_is_rejected(client, admin_headers):
    trigger = client.get("/v1/admin/triggers", headers=admin_headers).json()[0]
    response = client.post(
        "/v1/admin/fillers",
        json={"title": "x", "text": "y", "trigger_id": trigger["id"], "engine": "elevenlabs"},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_switching_engine_invalidates_generated_audio(client, admin_headers):
    """Vorhandenes Audio stammt von der alten Engine - beim Wechsel muss es
    weg, sonst spielt der Turn weiter die alte Stimme."""
    from orchestrator.services import filler_service

    filler = _create_filler(client, admin_headers)
    path = filler_service.audio_path(filler["id"], "default-de-female")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())
    assert path.exists()

    client.put(
        f"/v1/admin/fillers/{filler['id']}",
        json={"title": filler["title"], "text": filler["text"],
              "trigger_id": filler["trigger_id"], "delay_ms": filler["delay_ms"],
              "engine": "piper"},
        headers=admin_headers,
    )

    assert not path.exists()


def test_filler_audio_can_be_played_back(client, admin_headers):
    """Play-Button im Panel: WAV mit Token abrufbar (ein <audio src> kann
    keinen Header senden, das Panel holt es per fetch als Blob)."""
    from orchestrator.services import filler_service

    filler = _create_filler(client, admin_headers)
    path = filler_service.audio_path(filler["id"], "default-de-female")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())

    response = client.get(
        f"/v1/admin/fillers/{filler['id']}/audio?voice_id=default-de-female",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"

    # Ohne Token kein Audio
    assert client.get(
        f"/v1/admin/fillers/{filler['id']}/audio?voice_id=default-de-female"
    ).status_code == 401


def test_playing_missing_audio_says_so(client, admin_headers):
    filler = _create_filler(client, admin_headers)
    response = client.get(
        f"/v1/admin/fillers/{filler['id']}/audio?voice_id=default-de-male",
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert "default-de-male" in response.json()["detail"]
