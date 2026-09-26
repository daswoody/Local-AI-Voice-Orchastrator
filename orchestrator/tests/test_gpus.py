"""GPU-Verteilung der Dienste (4.2, v1.14): Uebersicht im Admin-Panel und
Zuweisung einzelner Dienste auf Karte oder CPU."""

import pytest

from orchestrator import repos
from orchestrator.services import gpu_manager


# ---- Compute-Type-Empfehlung --------------------------------------------------


def test_older_cards_get_int8_instead_of_float16():
    """Pascal (GTX 10xx, Compute 6.1) emuliert float16 und ist damit
    langsamer als int8 - Turing und neuer koennen es nativ."""
    assert gpu_manager.recommended_compute_type(6.1) == "int8_float16"
    assert gpu_manager.recommended_compute_type(7.5) == "float16"  # 2080 Ti
    assert gpu_manager.recommended_compute_type(8.6) == "float16"
    # Unbekannte Capability -> konservativ der bisherige Default
    assert gpu_manager.recommended_compute_type(None) == "float16"


def test_missing_nvml_is_reported_not_raised(monkeypatch):
    """Ohne GPU-Freigabe im Orchestrator-Container bleibt das Panel
    bedienbar - es zeigt nur keine VRAM-Zahlen."""
    result = gpu_manager.list_gpus()

    assert result["available"] is False
    assert result["gpus"] == []
    assert result["reason"]


# ---- Uebersicht ----------------------------------------------------------------


def _unreachable_services(monkeypatch):
    async def fake_status(name):
        return {"reachable": False, "error": "Connection refused"}

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)


def test_gpu_overview_requires_admin(client):
    assert client.get("/v1/admin/gpus").status_code == 401


def test_overview_lists_all_services(client, admin_headers, monkeypatch):
    """Alle Dienste stehen in der Liste - auch die, die wir nicht
    umschalten koennen (Piper: CPU per Design, LM Studio: laeuft auf dem
    Host, Breeze: Karte fest per Compose)."""
    _unreachable_services(monkeypatch)

    body = client.get("/v1/admin/gpus", headers=admin_headers).json()

    services = {entry["name"]: entry for entry in body["services"]}
    # tts-qwen3: vorbelegte Engine nach dem Engine-Vertrag (v1.20)
    assert set(services) == {"stt", "tts-xtts", "tts-breeze", "tts-piper", "llm", "tts-qwen3"}
    assert services["tts-qwen3"]["controllable"] is True and services["tts-qwen3"]["can_disable"] is True
    assert services["tts-breeze"]["controllable"] is False
    assert "BREEZE_GPU" in services["tts-breeze"]["control_hint"]
    assert services["stt"]["controllable"] is True
    assert services["tts-xtts"]["controllable"] is True
    assert services["tts-piper"]["controllable"] is False
    assert services["tts-piper"]["effective"] == "cpu"
    assert services["llm"]["external"] is True
    # Der Hinweis sagt dem Admin, wo er die LLM-Karte stattdessen einstellt
    assert "LM Studio" in services["llm"]["note"]


def test_overview_shows_live_device_of_reachable_services(client, admin_headers, monkeypatch):
    async def fake_status(name):
        if name == "stt":
            return {"reachable": True, "assigned": "cuda:1", "effective": "cpu",
                    "loaded": True, "compute_type": "float16"}
        return {"reachable": False, "error": "down"}

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)

    body = client.get("/v1/admin/gpus", headers=admin_headers).json()
    stt = next(s for s in body["services"] if s["name"] == "stt")

    # Zugewiesen cuda:1, laeuft aber auf CPU -> genau diese Abweichung soll
    # das Panel anzeigen koennen (CPU-Fallback bei vollem VRAM).
    assert stt["assigned"] == "cuda:1"
    assert stt["effective"] == "cpu"


# ---- Zuweisung -------------------------------------------------------------------


def test_assign_device_calls_service_and_persists(client, admin_headers, monkeypatch):
    calls = []

    async def fake_set(name, device, compute_type=None):
        calls.append((name, device, compute_type))
        return {"assigned": device, "effective": device, "loaded": True}

    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)
    _unreachable_services(monkeypatch)

    response = client.put("/v1/admin/gpus/tts-xtts", headers=admin_headers,
                          json={"device": "cuda:1"})

    assert response.status_code == 200
    assert response.json()["effective"] == "cuda:1"
    assert calls == [("tts-xtts", "cuda:1", None)]
    # Gemerkt -> ueberlebt einen Container-Neustart
    assert gpu_manager.assigned_device("tts-xtts") == "cuda:1"


def test_assign_passes_compute_type_matching_the_card(client, admin_headers, monkeypatch):
    """Der Orchestrator kennt die Karte (NVML) und gibt dem STT-Dienst den
    passenden Compute-Type mit - der Dienst muss die Hardware nicht kennen."""
    calls = []

    async def fake_set(name, device, compute_type=None):
        calls.append((name, device, compute_type))
        return {"assigned": device, "effective": device}

    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)
    monkeypatch.setattr(gpu_manager, "list_gpus", lambda: {
        "available": True, "reason": "",
        "gpus": [
            {"index": 0, "device": "cuda:0", "compute_type": "float16"},
            {"index": 1, "device": "cuda:1", "compute_type": "int8_float16"},
        ],
    })
    _unreachable_services(monkeypatch)

    client.put("/v1/admin/gpus/stt", headers=admin_headers, json={"device": "cuda:1"})

    assert calls == [("stt", "cuda:1", "int8_float16")]


def test_non_controllable_and_unknown_services_are_rejected(client, admin_headers):
    piper = client.put("/v1/admin/gpus/tts-piper", headers=admin_headers,
                       json={"device": "cuda:0"})
    assert piper.status_code == 400

    llm = client.put("/v1/admin/gpus/llm", headers=admin_headers, json={"device": "cuda:0"})
    assert llm.status_code == 400
    assert "LM Studio" in llm.json()["detail"]

    assert client.put("/v1/admin/gpus/quatsch", headers=admin_headers,
                      json={"device": "cuda:0"}).status_code == 404


def test_invalid_device_is_rejected_before_any_call(client, admin_headers):
    assert client.put("/v1/admin/gpus/stt", headers=admin_headers,
                      json={"device": "gpu5"}).status_code == 422


def test_service_error_is_passed_through(client, admin_headers, monkeypatch):
    """Karte voll: Der Dienst sagt selbst, woran es lag - das gehoert
    unveraendert ins Panel, nicht als anonymer 500er."""
    async def fake_set(name, device, compute_type=None):
        raise RuntimeError("stt hat den Wechsel auf cuda:1 abgelehnt (500): CUDA out of memory")

    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)
    _unreachable_services(monkeypatch)

    response = client.put("/v1/admin/gpus/stt", headers=admin_headers,
                          json={"device": "cuda:1"})

    assert response.status_code == 502
    assert "CUDA out of memory" in response.json()["detail"]


# ---- Wiederherstellung nach Neustart ----------------------------------------------


@pytest.mark.asyncio
async def test_restore_reapplies_assignment_after_service_restart(monkeypatch):
    """Ein neu gestarteter Dienst meldet seinen Compose-Default - dann muss
    die gespeicherte Zuweisung nachgezogen werden."""
    repos.set_setting("gpu_device_stt", "cuda:1")
    calls = []

    async def fake_status(name):
        return {"reachable": True, "assigned": "cuda:0", "effective": "cuda:0"}

    async def fake_set(name, device, compute_type=None):
        calls.append((name, device))
        return {}

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)
    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)

    await gpu_manager.restore_assignments()

    assert calls == [("stt", "cuda:1")]


@pytest.mark.asyncio
async def test_restore_does_not_undo_an_active_cpu_fallback(monkeypatch):
    """Der Dienst steht auf der zugewiesenen Karte, ist aber auf CPU
    ausgewichen. Wuerden wir hier neu zuweisen, drehten wir den Fallback in
    einer Endlosschleife zurueck."""
    repos.set_setting("gpu_device_stt", "cuda:1")
    calls = []

    async def fake_status(name):
        return {"reachable": True, "assigned": "cuda:1", "effective": "cpu"}

    async def fake_set(name, device, compute_type=None):
        calls.append((name, device))
        return {}

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)
    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)

    await gpu_manager.restore_assignments()

    assert calls == []


@pytest.mark.asyncio
async def test_restore_ignores_unreachable_services(monkeypatch):
    repos.set_setting("gpu_device_stt", "cuda:1")

    async def fake_status(name):
        return {"reachable": False, "error": "down"}

    async def boom(*args, **kwargs):
        raise AssertionError("darf nicht aufgerufen werden")

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)
    monkeypatch.setattr(gpu_manager, "set_service_device", boom)

    await gpu_manager.restore_assignments()  # darf nicht werfen


# ---- XTTS ausschalten (v1.19) ----------------------------------------------------


def _recording_set(monkeypatch) -> list:
    calls = []

    async def fake_set(name, device, compute_type=None):
        calls.append((name, device))
        return {"assigned": device, "effective": None, "loaded": False}

    monkeypatch.setattr(gpu_manager, "set_service_device", fake_set)
    return calls


def test_xtts_stays_on_while_it_speaks_the_main_answer(client, admin_headers, monkeypatch):
    """Ohne aktive Alternative waere die Assistenz stumm - erst eine andere
    Engine aktivieren, dann ausschalten."""
    calls = _recording_set(monkeypatch)

    response = client.put("/v1/admin/gpus/tts-xtts", headers=admin_headers, json={"device": "off"})

    assert response.status_code == 409
    assert "aktive Hauptstimme" in response.json()["detail"]
    assert calls == []
    assert not gpu_manager.is_off("tts-xtts")


def test_xtts_can_be_switched_off_and_the_choice_is_remembered(client, admin_headers, monkeypatch):
    from orchestrator.services import tts_engines

    tts_engines.set_active_engine("breeze")
    calls = _recording_set(monkeypatch)
    _unreachable_services(monkeypatch)

    response = client.put("/v1/admin/gpus/tts-xtts", headers=admin_headers, json={"device": "off"})

    assert response.status_code == 200
    assert calls == [("tts-xtts", "off")]
    assert gpu_manager.is_off("tts-xtts")
    services = {s["name"]: s for s in client.get("/v1/admin/gpus", headers=admin_headers).json()["services"]}
    assert services["tts-xtts"]["can_disable"] is True
    assert services["stt"]["can_disable"] is False


def test_only_services_that_can_unload_accept_off(client, admin_headers, monkeypatch):
    calls = _recording_set(monkeypatch)

    response = client.put("/v1/admin/gpus/stt", headers=admin_headers, json={"device": "off"})

    assert response.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_restore_switches_xtts_off_again_after_a_restart(monkeypatch):
    """Nach einem Neustart meldet XTTS seinen Default (noch nichts geladen) -
    die Zuweisung "off" wird nachgezogen."""
    repos.set_setting("gpu_device_tts-xtts", "off")
    calls = _recording_set(monkeypatch)

    async def fake_status(name):
        return {"reachable": True, "assigned": "cuda:0", "effective": None, "loaded": False}

    monkeypatch.setattr(gpu_manager, "service_status", fake_status)

    await gpu_manager.restore_assignments()

    assert ("tts-xtts", "off") in calls
